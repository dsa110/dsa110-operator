"""Read-only etcd facade tests (against the in-memory fake)."""
from dsa_operator.etcd.read import FakeEtcdReader, ReadOnlyEtcd


def test_get_dict_decodes_json():
    r = ReadOnlyEtcd(FakeEtcdReader({"/mon/array/dec": {"dec_deg": 16.27}}))
    assert r.get_dict("/mon/array/dec") == {"dec_deg": 16.27}


def test_missing_key_is_none():
    r = ReadOnlyEtcd(FakeEtcdReader())
    assert r.get_dict("/mon/nope") is None


def test_get_prefix_dict():
    r = ReadOnlyEtcd(FakeEtcdReader({
        "/mon/service/corr_rt/3": {"up": True},
        "/mon/service/corr_rt/4": {"up": True},
        "/mon/other": {"x": 1},
    }))
    got = r.get_prefix_dict("/mon/service/corr_rt/")
    assert set(got) == {"/mon/service/corr_rt/3", "/mon/service/corr_rt/4"}


def test_non_json_value_falls_back_to_string():
    fake = FakeEtcdReader()
    fake.set("/mon/raw", b"not-json")
    r = ReadOnlyEtcd(fake)
    assert r.get_dict("/mon/raw") == "not-json"


def test_facade_has_no_write_methods():
    # Read-only by construction: no put/delete/lease/watch on the facade.
    r = ReadOnlyEtcd(FakeEtcdReader())
    for forbidden in ("put", "put_dict", "delete", "lease", "add_watch"):
        assert not hasattr(r, forbidden)
