from datetime import datetime, timedelta, timezone

from memory_core.abilities import Ability, abilities_block, gained_note, lost_note, notice

T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
MEM = Ability("m:memory", "翻找记忆", "想不起来时翻一翻")
PIC = Ability("m:vision", "看图", "发来的图会讲给你听")
WEB = Ability("w:web", "上网逛逛", "每天出门几次")


def test_first_list_is_the_baseline(core):
    n = notice(core, [MEM, WEB], now=T0)
    assert n.first and not n.gained and not n.fresh
    again = notice(core, [MEM, WEB], now=T0 + timedelta(minutes=1))
    assert not again.first and not again.gained and not again.lost


def test_new_ability_is_noticed_once_and_fresh_for_a_while(core):
    notice(core, [MEM], now=T0)
    n = notice(core, [MEM, PIC], now=T0 + timedelta(hours=1))
    assert n.gained == [PIC] and n.fresh == {"m:vision"}
    later = notice(core, [MEM, PIC], now=T0 + timedelta(hours=5))
    assert not later.gained and later.fresh == {"m:vision"}  # noticed once, still new
    old = notice(core, [MEM, PIC], now=T0 + timedelta(hours=30))
    assert not old.fresh


def test_lost_ability_is_noticed_and_comes_back(core):
    notice(core, [MEM, WEB], now=T0)
    n = notice(core, [MEM], now=T0 + timedelta(hours=1))
    assert n.lost == [WEB] and [a.key for a in n.gone] == ["w:web"]
    assert not notice(core, [MEM], now=T0 + timedelta(hours=2)).lost
    back = notice(core, [MEM, WEB], now=T0 + timedelta(hours=3))
    assert back.gained == [WEB] and not back.gone


def test_without_update_nothing_is_written(core):
    notice(core, [MEM], now=T0)
    peek = notice(core, [MEM, PIC], now=T0 + timedelta(hours=1), update=False)
    assert peek.gained == [PIC]
    assert notice(core, [MEM, PIC], now=T0 + timedelta(hours=2)).gained == [PIC]  # still to be noticed


def test_rewording_is_not_new(core):
    notice(core, [MEM], now=T0)
    n = notice(core, [Ability("m:memory", "翻找记忆", "换了个说法")], now=T0 + timedelta(hours=1))
    assert not n.gained and not n.lost and not n.fresh


def test_block_and_notes(core):
    notice(core, [MEM, WEB], now=T0)
    n = notice(core, [MEM, PIC], now=T0 + timedelta(hours=1))
    block = abilities_block([MEM, PIC], n, owner="老板")
    assert "看图（刚有的" in block and "不能再上网逛逛" in block and "告诉老板" in block
    assert "刚有的" not in abilities_block([MEM])
    assert abilities_block([]) == ""
    assert gained_note(PIC).startswith("我发现自己多了一项本事：看图")
    assert lost_note(WEB) == "我发现自己不能再上网逛逛了"
