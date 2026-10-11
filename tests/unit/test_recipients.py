import pytest

from revos.policy.recipients import base_of, is_plus_variant, recipient_allowed

BASE = "name@gmail.com"


@pytest.mark.parametrize("address", [
    "name@gmail.com", "NAME@Gmail.com", "name+acme@gmail.com", "name+a.b-c_1@gmail.com", " name+x1@gmail.com "])
def test_the_base_address_and_its_plus_tags_are_allowed(address) -> None:
    assert recipient_allowed(address, set(), {BASE})


@pytest.mark.parametrize("address", [
    "name2@gmail.com",            # a different local part
    "name+x@other.com",           # a different domain
    "name.x+tag@gmail.com",       # dots are not ignored: only the exact local part counts
    "n.ame@gmail.com", "xname@gmail.com", "name-x@gmail.com", "namex+tag@gmail.com",
    "name+@gmail.com",            # an empty tag
    "name+a+b@gmail.com",         # a second plus
    "name+ta g@gmail.com", "name+tag@gmail.com.evil.com", "name+tag@mail.gmail.com",
    "name+tag@gmail.com@evil.com", "name@gmail.com@evil.com", "+tag@gmail.com", "@gmail.com", "name", "",
    "name+t@gmail.co", "Name+Tag@googlemail.com"])
def test_near_misses_are_blocked(address) -> None:
    assert not recipient_allowed(address, set(), {BASE})


def test_exact_addresses_still_work_and_nothing_is_allowed_without_a_base() -> None:
    assert recipient_allowed("owner@x.org", {"owner@x.org"}, {BASE})
    assert not recipient_allowed("owner+tag@x.org", {"owner@x.org"}, {BASE})      # exact means exact
    assert not recipient_allowed("name+acme@gmail.com", set(), set())
    assert not recipient_allowed("name+acme@gmail.com", set(), {""})


def test_a_base_that_itself_has_a_tag_is_reduced_to_its_local_part() -> None:
    assert is_plus_variant("name+acme@gmail.com", "name+demo@gmail.com")
    assert base_of("Name+Acme@Gmail.com") == "name@gmail.com" and base_of("nonsense") == ""
