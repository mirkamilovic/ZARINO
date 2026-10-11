from aiogram.fsm.state import StatesGroup, State


class Link(StatesGroup):
    ch = State()


class NewB(StatesGroup):
    ch = State()
    title = State()
    rules = State()
    points = State()
    end = State()


class Join(StatesGroup):
    nick = State()


class Rnd(StatesGroup):
    text = State()
    points = State()


class Pts(StatesGroup):
    amount = State()


class Wal(StatesGroup):
    addr = State()


class OSet(StatesGroup):
    val = State()


class OImg(StatesGroup):
    photo = State()


class OItem(StatesGroup):
    kind = State()
    title = State()
    descr = State()
    price = State()
    payload = State()


class OBc(StatesGroup):
    msg = State()


class Rcpt(StatesGroup):
    photo = State()


class GameBuy(StatesGroup):
    pid = State()
    srv = State()


class OGame(StatesGroup):
    name = State()
    server = State()


class OPack(StatesGroup):
    bulk = State()
    price = State()
