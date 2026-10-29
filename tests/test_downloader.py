from rkm_clips import downloader


class FakeYDL:
    seen = []

    def __init__(self, opts):
        FakeYDL.seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        return {"url": "https://x/stream.m3u8"}


def test_network_options_retry_with_growing_waits():
    opts = downloader._network_opts()
    assert opts["retries"] >= 10 and opts["extractor_retries"] >= 8 and opts["socket_timeout"] == 30
    waits = [opts["retry_sleep_functions"]["extractor"](n) for n in range(8)]
    assert waits == sorted(waits) and waits[0] <= 2 and max(waits) <= 30    # crece y tiene tope


def test_stream_url_also_retries(monkeypatch):
    FakeYDL.seen.clear()
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    assert downloader.stream_url("https://twitch.tv/videos/1", "best") == "https://x/stream.m3u8"
    assert FakeYDL.seen[0]["extractor_retries"] >= 8
