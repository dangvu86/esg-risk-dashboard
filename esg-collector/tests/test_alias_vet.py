"""alias_vet tests — verdict logic + --apply. Run: python -m tests.test_alias_vet"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _texts():
    # 6 articles mentioning the collision word "Hoàn Vũ" with NO Novaland anchor
    out = [f"Hoa hậu Hoàn Vũ {i}: đêm chung kết rực rỡ" for i in range(6)]
    # 6 articles using ONLY the dominant brand (a `names` entry) — must cap at
    # REVIEW, never FAIL (the VietinBank blind spot)
    out += [f"Novahomes mở bán đợt {i} tại Đồng Nai" for i in range(6)]
    # 3 articles where the brand co-occurs with the parent company
    out += [
        "Novaland duyệt bán Công ty TNHH Ngôi nhà Mega",
        "NVL dồn vốn vào Ngôi Nhà Mega trước khi thoái",
        "Tập đoàn Đầu tư Địa ốc No Va cơ cấu Ngôi Nhà Mega",
    ]
    # 2 hits for a low-volume alias with zero co-occurrence → REVIEW, not FAIL
    out += ["Giải bóng đá Trúc Quỳnh mở rộng", "Cô giáo Trúc Quỳnh nhận giải"]
    out += ["Thị trường chứng khoán hôm nay tăng điểm"] * 5
    return out


def _pool_dir(td: Path) -> Path:
    ad = td / "aliases"; ad.mkdir()
    (ad / "NVL.json").write_text(json.dumps({
        "ticker": "NVL", "company_name": "CTCP Tập đoàn Đầu tư Địa ốc No Va",
        "names": ["NVL", "Novaland", "Đầu tư Địa ốc No Va", "Novahomes"],
        "subsidiaries": ["Hoàn Vũ", "Ngôi Nhà Mega", "Đầu tư Trúc Quỳnh", "Trúc Quỳnh"],
        "projects": [], "locations": [],
    }, ensure_ascii=False), encoding="utf-8")
    return ad


def test_verdicts():
    from alias_builder import alias_vet
    with tempfile.TemporaryDirectory() as td:
        ad = _pool_dir(Path(td))
        verdicts = {v.alias: v for v in alias_vet.vet_ticker("NVL", _texts(), aliases_dir=ad)}
        assert verdicts["Hoàn Vũ"].verdict == "FAIL", verdicts["Hoàn Vũ"]       # 6 hits, 0% anchor
        assert verdicts["Hoàn Vũ"].samples                                       # samples shown
        assert verdicts["Ngôi Nhà Mega"].verdict == "PASS"                       # 3 hits, 100% anchor
        assert verdicts["Trúc Quỳnh"].verdict == "REVIEW"                        # 2 hits < FAIL_MIN_HITS
        assert verdicts["Đầu tư Trúc Quỳnh"].verdict == "PASS"                   # 0 hits
        assert verdicts["Novaland"].verdict == "PASS"                            # name, co-occurs
        assert verdicts["Novahomes"].verdict == "REVIEW"                         # names never FAIL
    print("  verdicts OK")


def test_apply_removes_only_fails():
    from alias_builder import alias_vet
    with tempfile.TemporaryDirectory() as td:
        ad = _pool_dir(Path(td))
        verdicts = alias_vet.vet_ticker("NVL", _texts(), aliases_dir=ad)
        removed = alias_vet.apply_fails("NVL", verdicts, aliases_dir=ad)
        assert removed == 1, removed
        pool = json.loads((ad / "NVL.json").read_text(encoding="utf-8"))
        assert "Hoàn Vũ" not in pool["subsidiaries"]
        assert "Ngôi Nhà Mega" in pool["subsidiaries"]      # PASS kept
        assert "Trúc Quỳnh" in pool["subsidiaries"]         # REVIEW never auto-removed
    print("  apply_removes_only_fails OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running alias_vet tests…")
    test_verdicts()
    test_apply_removes_only_fails()
    print("ALL OK")


if __name__ == "__main__":
    main()
