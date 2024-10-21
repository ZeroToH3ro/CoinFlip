import beaker
import pyteal as pt
import random

class CoinflipState:
    player_a_side = beaker.GlobalStateValue(
        stack_type = pt.TealType.bytes,
        descr = "First player's side of the coin",
        default = pt.Bytes("Not chosen yet"),
    )

    player_b_side = beaker.GlobalStateValue(
        stack_type = pt.TealType.bytes,
        descr = "Second player's side of the coin",
        default = pt.Bytes("Not chosen yet"),
    )

    player_a_account = beaker.GlobalStateValue(
        stack_type = pt.TealType.bytes,
        descr = "First player's account",
        default = pt.Bytes("Empty"),
    )

    player_b_account = beaker.GlobalStateValue(
        stack_type = pt.TealType.bytes,
        descr = "Second player's account",
        default = pt.Bytes("Empty"),
    )

    wager = beaker.GlobalStateValue(
        stack_type = pt.TealType.uint64,
        descr = "Betting amount for the coinflip",
        default = pt.Int(0),
    )

    player_games_won = beaker.LocalStateValue(
        stack_type = pt.TealType.uint64,
        descr = "Amount of coinflip games won",
        default = pt.Int(0),
    )

app = beaker.Application("coinflip", state=CoinflipState)

@app.external
def hello(name: pt.abi.String, *, output: pt.abi.String) -> pt.Expr:
    return output.set(pt.Concat(pt.Bytes("Hello, "), name.get()))

@app.create(bare=True)
def create() -> pt.Expr:
    """ Method for creating the smart contract, intializing global state """

    return app.initialize_global_state()

@app.opt_in(bare=True)
def opt_in() -> pt.Expr:
    """ Optin method """

    return pt.Seq(

        # During optin we fill in the slots for the game (first to optin is player A, second is player B, the rest are disallowed from opting in)
        pt.If(app.state.player_a_account.get() == pt.Bytes("Empty"))
        .Then(app.state.player_a_account.set(pt.Txn.sender()))
        .ElseIf(app.state.player_b_account.get() == pt.Bytes("Empty"))
        .Then(app.state.player_b_account.set(pt.Txn.sender()))
        .Else(pt.Reject()),

        app.initialize_local_state(addr=pt.Txn.sender())
    )

@app.external(authorize=beaker.Authorize.opted_in())
def start_game(payment: pt.abi.PaymentTransaction, choice: pt.abi.String, *, output: pt.abi.String) -> pt.Expr:
    """
    Player A initiates the game, he pays the wager he wants for the game and then chooses the side of the coin ("Heads" or "Tails")
    """

    return pt.Seq(
        pt.Assert(
            pt.And(
                payment.type_spec().txn_type_enum() == pt.TxnType.Payment, # Making sure it's the correct TRANSACTION TYPE
                app.state.wager.get() == pt.Int(0),
                payment.get().receiver() == pt.Global.current_application_address(), # Making sure the user put the correct application address
                app.state.player_a_side.get() == pt.Bytes("Not chosen yet"),

                # Checks whether the userer inputed a side for the coinflip correctly (heads/tails in any form should be allowed)
                check_correct_input(choice.get()) == pt.Int(1),
            )

        ),

        app.state.player_a_account.set(pt.Txn.sender()),
        app.state.wager.set(payment.get().amount()),
    )

@app.external(authorize=beaker.Authorize.opted_in())
def join_game(payment: pt.abi.PaymentTransaction, *, output: pt.abi.String) -> pt.Expr:
    """
    Player B joins the game, gets the side that's left (player A picks first), pays the wager and waits for the game to resolve by player A
    """

    return pt.Seq(
        pt.Assert(
            pt.And(
                payment.type_spec().txn_type_enum() == pt.TxnType.Payment,

                app.state.player_a_account != pt.Txn.sender(), # With this we disallow player A to fill both slots
                app.state.player_b_side.get() == pt.Bytes("Not chosen yet"),

                payment.get().receiver() == pt.Global.current_application_address(),
                app.state.wager.get() == payment.get().amount(),
            )
        ),

        app.state.player_b_account.set(pt.Txn.sender()),
        app.state.player_b_side.set(leftover_side(app.state.player_a_side.get())),
    )

@app.external(authorize=beaker.Authorize.opted_in())
def resolve_game(opp: pt.abi.Account, *, output: pt.abi.String) -> pt.Expr:
    """
    Player A resolves the game, win counter updates and the wager pays out to the player who won
    """

    winner = pt.ScratchVar(pt.TealType.uint64)
    
    return pt.Seq(

        pt.Assert(
            pt.And(
                app.state.player_a_account.get() == pt.Txn.sender(),
                app.state.player_b_account.get() != pt.Bytes("Empty"),
            )
        ),

        winner.store(decide_winner()),
        payout(app.state.wager.get(), winner.load()),

        # Incrementing the counter for games won
        app.state.player_games_won[pt.Txn.accounts[winner.load()]].set(app.state.player_games_won[pt.Txn.accounts[winner.load()]].get() + pt.Int(1)),

        # Resetting the game
        reset_game(),

        pt.Cond(
            [winner.load() == pt.Int(0), output.set(pt.Bytes("Player A won the coinflip."))],
            [winner.load() == pt.Int(1), output.set(pt.Bytes("Player B won the coinflip."))]
        )
    )

@app.external(authorize = beaker.Authorize.opted_in(), read_only=True)
def check_wins(*, output: pt.abi.Uint64) -> pt.Expr:
    """
    Check personal number of coinflip wins
    """

    return output.set(app.state.player_games_won.get())

@pt.Subroutine(pt.TealType.none)
def reset_game() -> pt.Expr:
    """
    Reset global state variables, two players that are already opted in can start a new game after this method executes
    """

    return pt.Seq(
        app.state.player_a_account.set(pt.Bytes("Empty")),
        app.state.player_b_account.set(pt.Bytes("Empty")),
        app.state.player_a_side.set(pt.Bytes("Not chosen yet")),
        app.state.player_b_side.set(pt.Bytes("Not chosen yet")),
        app.state.wager.set(pt.Int(0)),
    )

@pt.Subroutine(pt.TealType.uint64)
def decide_winner() -> pt.Expr:
    """
    Suborutine that chooses randomly between 1 (Heads) or 2 (Tails), and returns the index of the winner's account (0==A, 1==B)
    """

    # This is done in vanilla python because nothing here needs to be saved no the chain, just a quick way to process the winner
    rng = random.randint(1,2)
    rng = "Heads" if rng==1 else "Tails"

    return pt.Seq(
        pt.If(app.state.player_a_side.get() == pt.Bytes(rng))
        .Then(pt.Int(0))
        .Else(pt.Int(1))
    )

@pt.Subroutine(pt.TealType.none)
def payout(wager: pt.Expr, winner_index: pt.Expr) -> pt.Expr:
    """
    Subroutine that pays out the winner
    """

    return pt.InnerTxnBuilder.Execute(
        {
            pt.TxnField.type_enum: pt.TxnType.Payment,
            pt.TxnField.amount: wager*pt.Int(2) - pt.Txn.fee(),
            pt.TxnField.receiver: pt.Txn.accounts[winner_index],
        }
    )

@pt.Subroutine(pt.TealType.bytes)
def leftover_side(player_a_side: pt.Expr) -> pt.Expr:
    """
    Simple helper function that returns the leftover side of the coin
    """

    return pt.Seq(
        pt.If(player_a_side == pt.Bytes("Heads"))
        .Then(pt.Bytes("Tails"))
        .Else(pt.Bytes("Heads"))
    )

@pt.Subroutine(pt.TealType.uint64)
def check_correct_input(input: pt.Expr) -> pt.Expr:
    """
    Function that checks whether the input is correct when choosing a side for the coinflip
    Any form is accepted if the word is correctly spelled (HEADS, heads, HEadS, etc.)
    Returns pt.Int(1) if the input is correct, otherwise pt.Int(0)
    """

    input_tolower = pt.ScratchVar(pt.TealType.bytes)

    return pt.Seq(
        input_tolower.store(to_lower(input)),

        pt.If(
            pt.Or(
                input_tolower.load() == pt.Bytes("heads"),
                input_tolower.load() == pt.Bytes("tails"),
            )
        )
        .Then(
            pt.Cond(
                [input_tolower.load() == pt.Bytes("heads"), app.state.player_a_side.set(pt.Bytes("Heads"))],
                [input_tolower.load() == pt.Bytes("tails"), app.state.player_a_side.set(pt.Bytes("Tails"))],
            ),

            pt.Int(1),
        )
        .Else(
            pt.Int(0),
        )
    )

@pt.Subroutine(pt.TealType.bytes)
def to_lower(byteslice: pt.Expr) -> pt.Expr:
    """
    Wrapper for the recursive function
    """

    return rtl(byteslice, pt.Int(0), pt.Len(byteslice))

@pt.Subroutine(pt.TealType.bytes)
def rtl(byteslice: pt.Expr, index: pt.Expr, len: pt.Expr) -> pt.Expr:
    """
    "rtl" stands for "Recursively to lower", if we encounter any Byte in the byteslice that is between 65 ("A") and 90 ("Z"),
    we simply set that byte to it's lowercase equal (+32 in the ascii table)
    """

    curr_byte = pt.ScratchVar(pt.TealType.uint64)

    return pt.Seq(
        pt.If( index >= len )
        .Then(
            byteslice,
        ).Else(
            curr_byte.store(pt.GetByte(byteslice, index)),

            pt.If(
                pt.And(
                    curr_byte.load() >= pt.Int(65),
                    curr_byte.load() <= pt.Int(90),
                )
            )
            .Then(
                rtl(pt.SetByte(byteslice, index, curr_byte.load()+pt.Int(32)), index+pt.Int(1), len)
            )
            .Else(
                rtl(byteslice, index+pt.Int(1), len)
            )
        )
    )
