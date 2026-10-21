import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fp5_unlock_stats as st  # noqa: E402

A_LOG = """AUTH 1 arm mode=1 irq=10
AUTH 1 REAL DOWN itype=0x2
CAP cmd=0x1013 rc=0 itype=0x0 esd=0 avgv=344 req_b=1536 rsp_b=2048
CAP cmd=0x1013 rc=0 itype=0x0 esd=0 avgv=325 req_b=1536 rsp_b=2048
CAP cmd=0x1013 rc=0 itype=0x0 esd=0 avgv=314 req_b=1536 rsp_b=2048
AUTH 1 burst avgv=344,325,314
REPORT_EV5 cmd=0x1017 rc=-11 itype=0x0 esd=0 avgv=0 req_b=1536 rsp_b=2048
REPORT_EV5 qsee: [b038660dca]<1>tzappfingerprint: D focaltech:libfp: FtVerifyByTemplate...FtVerifySubTemplate() score = 0, matchCnts = 40
REPORT_EV5 qsee: [b038666081]<1>tzappfingerprint: D focaltech:ta:core: image quality = 88, coverage = 99, humidity = 95.
AUTH 1 report rc=-11 fid=0
AUTH 1 burst avgv=299,288,275
REPORT_EV5 cmd=0x1017 rc=0 itype=0x0 esd=0 avgv=0 req_b=1536 rsp_b=2048
REPORT_EV5 qsee: [b038d9a000]<1>tzappfingerprint: D focaltech:libfp: FtVerifyByTemplate...FtVerifySubTemplate() score = 12, matchCnts = 9
REPORT_EV5 qsee: [b038d9a100]<1>tzappfingerprint: I focaltech:ta:core: authenticated fid = 1979259351, gid = 0.
AUTH 1 report rc=0 fid=1979259351
AUTH HIT 1 itype=0x2 avgv=275 fid=1979259351 rc=0
AUTH 2 arm mode=1 irq=12
AUTH 2 already down itype=0x2
AUTH 2 REAL DOWN itype=0x2
AUTH 2 burst avgv=300,300,300
REPORT_EV5 cmd=0x1017 rc=-11 itype=0x0 esd=0 avgv=0 req_b=1536 rsp_b=2048
AUTH FAIL 2 itype=0x2 esd=0 avgv=300 fid=0 rc=-11
"""

B_LOG = """TIME 0 start 0 wall=1792000000.000
TUNE scans=3 rescan_ms=1000 skip_falling=0
TIME 1 arm 2100
AUTH 1 arm mode=1 irq=10
AUTH 1 REAL DOWN itype=0x2
TIME 1 down 5000
TIME 1 caps 5150 scan=1
AUTH 1 burst avgv=344,325,314
REPORT_EV5 cmd=0x1017 rc=0 itype=0x0 esd=0 avgv=0 req_b=1536 rsp_b=2048
TIME 1 report 5190 scan=1
AUTH 1 report rc=0 fid=7
TIME 1 hit 5191 scan=1
AUTH HIT 1 itype=0x2 avgv=314 fid=7 rc=0
"""


def test_counts_and_ticks(tmp_path):
    p = tmp_path / "finger-unlock-20261021-191116.txt"
    p.write_text(A_LOG)
    r = st.parse_file(p)
    assert r["variant"] == "A(no TUNE)"
    assert [x["result"] for x in r["presses"]] == ["hit", "fail"]
    hit = r["presses"][0]
    assert len(hit["bursts"]) == 2 and hit["bursts"][1]["authed"] == 1979259351
    assert hit["bursts"][0]["q"] == 88
    assert r["presses"][1]["wake"] is True
    rows = st.summarize([r], lambda r: r["kind"])
    assert "1/2 = 50%" in rows[1]
    # burst-to-burst from TA ticks: 0xb038d9a000 - 0xb038660dca at 19.2 MHz
    gap = (0xb038d9a000 - 0xb038660dca) / st.TICK_HZ * 1000
    assert rows[1].endswith(f"{gap:.0f}")


def test_time_lines(tmp_path):
    p = tmp_path / "finger-unlock-20261022-070000.txt"
    p.write_text(B_LOG)
    r = st.parse_file(p)
    assert r["variant"].startswith("B(scans=3")
    assert r["presses"][0]["down_to_end_ms"] == 191
    assert r["presses"][0]["scan_ms"] == [190]


def test_cli(tmp_path, capsys):
    (tmp_path / "finger-unlock-20261021-191116.txt").write_text(A_LOG)
    (tmp_path / "finger-unlock-20261022-070000.txt").write_text(B_LOG)
    assert st.main([str(tmp_path), "--since", "2026-10-22T00:00"]) == 0
    out = capsys.readouterr().out
    assert "B(scans=3" in out and "A(no TUNE)" not in out
