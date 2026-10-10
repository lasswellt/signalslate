"""Tests for pipeline.packages.html: link/text harvesting and wrapper decoding."""
import base64
import json

from pipeline.packages.html import harvest


def _toks(h):
    return {(c.token, c.carrier_hint) for c in h.url_candidates}


def test_awstrack_swiship_tba_decoded():
    url = "https://xxxx.r.us-east-1.awstrack.me/L0/https:%2F%2Fwww.swiship.com%2Ftrack%3Fid%3DTBA000000000001/1/OPAQUE"
    h = harvest(f'<a href="{url}">Track</a>', None)
    assert ("TBA000000000001", "amazon") in _toks(h)
    assert any(l.startswith("https://www.swiship.com/track") for l in h.links)
    assert url in h.links


def test_mandrill_p_base64_decoded():
    inner = json.dumps({"url": "https://www.ups.com/track?tracknum=1z9x9x9x0326709847"})
    p = base64.urlsafe_b64encode(inner.encode()).decode().rstrip("=")
    h = harvest(f'<a href="https://mandrillapp.com/track/click/123/x.test?p={p}">go</a>', None)
    assert ("1Z9X9X9X0326709847", "ups") in _toks(h)


def test_narvar_upsn_param():
    u = "https://tracking.narvar.com/example/tracking/UPSN?tracking_numbers=1Z9X9X9X0326709847&service=E1"
    assert ("1Z9X9X9X0326709847", "ups") in _toks(harvest(None, f"see {u}"))


def test_ups_tracknum():
    u = "https://www.ups.com/track?loc=en_US&tracknum=1Z9X9X9X0326709847"
    assert _toks(harvest(f'<a href="{u}">x</a>', None)) == {("1Z9X9X9X0326709847", "ups")}


def test_fedex_trknbr():
    u = "https://www.fedex.com/apps/fedextrack?trknbr=753045655424&intcmp=notifType:1019"
    assert ("753045655424", "fedex") in _toks(harvest(f'<a href="{u}">x</a>', None))


def test_comma_list_split():
    u = "https://store.test/t?nums=YT0000000000000001,YT0000000000000002"
    assert len(harvest(None, u).url_candidates) == 2


def test_generic_id_on_non_tracking_host_ignored():
    h = harvest('<a href="https://example-store.test/order?id=ABCDEF1234567">x</a>', None)
    assert h.url_candidates == ()
    assert "https://example-store.test/order?id=ABCDEF1234567" in h.links


def test_opaque_wrapper_left_alone():
    h = harvest('<a href="https://w-mt.co/g/x?redirectTo=OPAQUE&iv=OPAQUE">x</a>', None)
    assert h.url_candidates == ()


def test_glued_text_prevented():
    h = harvest("<p>Tracking #:</p><table><tr><td>1541729044</td><td>Track</td></tr></table>", None)
    assert "1541729044Track" not in h.text
    assert "1541729044 Track" in h.text or "1541729044\nTrack" in h.text


def test_script_style_hidden_dropped():
    h = harvest('<style>.a{}</style><script>var x=1</script><div style="display:none">SECRET</div>'
                "<span>visible</span>", None)
    assert h.text == "visible"


def test_markdown_link_in_plain_text():
    h = harvest(None, "Track [here](https://www.ups.com/track?tracknum=1Z9X9X9X0326709847) now")
    assert ("1Z9X9X9X0326709847", "ups") in _toks(h)


def test_goshippo_path_hint():
    h = harvest(None, "https://goshippo.com/track/usps/9400111899223197428490")
    assert ("9400111899223197428490", "usps") in _toks(h)


def test_empty_input():
    h = harvest(None, None)
    assert h.text == "" and h.links == () and h.url_candidates == ()
