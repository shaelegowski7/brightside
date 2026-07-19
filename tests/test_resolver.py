from app.resolver import thread_id_from_url


def test_thread_id_from_real_url():
    url = "https://www.hotukdeals.com/deals/jameson-original-triple-distilled-blended-irish-whiskey-70cl-4938754"
    assert thread_id_from_url(url) == "4938754"


def test_thread_id_from_url_with_trailing_slash():
    url = "https://www.hotukdeals.com/deals/some-slug-123456/"
    assert thread_id_from_url(url) == "123456"


def test_thread_id_returns_none_for_non_thread_url():
    assert thread_id_from_url("https://www.hotukdeals.com/hot") is None
