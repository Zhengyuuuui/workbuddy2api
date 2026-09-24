"""extract_flows_token 单元测试：mock subprocess，验证过滤/去重/输出格式/0600。

不调用真实 mitmdump、不读真实 flows、不打印完整 token。
"""

import json
import subprocess

import pytest

import extract_flows_token as eft

TOKEN = "v3:" + "A" * 120
TOKEN_OTHER = "v3:" + "B" * 140


def _patch_runner(monkeypatch, entries, returncode=0, exc=None):
    """monkeypatch _run_mitmdump：把给定 entries 写入 jsonl（模拟 addon 产物）。"""
    def _fake(flows_path, addon_path, jsonl_path):
        if exc is not None:
            raise exc
        if entries:
            jsonl_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess([], returncode, "", "")
    monkeypatch.setattr(eft, "_run_mitmdump", _fake)


def _entry(token=TOKEN, path="/v2/chat/completions", uid="f400a4f2cafe"):
    return {"token": token, "uid": uid, "path": path}


class TestFilterEntries:
    def test_filters_by_path_and_prefix_and_dedupes(self):
        entries = [
            _entry(),                                   # 命中
            _entry(),                                   # 同 token 重复 → 去重
            _entry(TOKEN_OTHER),                        # 不同 token → 保留
            _entry(path="/v1/models"),                  # 路径不匹配
            _entry(token="not-a-token"),                # 非 v3: 前缀
            _entry(token="v3:short"),                   # 过短
        ]
        out = eft._filter_entries(entries, "/v2/chat/completions")
        assert [e["token"] for e in out] == [TOKEN, TOKEN_OTHER]

    def test_empty_input(self):
        assert eft._filter_entries([], "/v2/chat/completions") == []


class TestHappyPath:
    def test_extracts_saves_json_0600_and_masks(self, tmp_path, monkeypatch, capsys):
        _patch_runner(monkeypatch, [
            _entry(),
            _entry(),                     # 重复：验证去重
            _entry(path="/v1/models"),    # 路径不匹配
        ])
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"fake flows")
        out = tmp_path / "tok.json"

        code = eft.main([str(flows), "--output", str(out)])
        assert code == 0

        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["token"] == TOKEN
        assert data["source"] == f"mitm:{flows}"
        assert data["_warning"] == "per-device secret, never share"
        assert data["found_at"]
        assert out.stat().st_mode & 0o777 == 0o600

        captured = capsys.readouterr()
        assert TOKEN not in captured.out and TOKEN not in captured.err
        assert TOKEN[:8] in captured.out          # 只显示前 8 字符
        assert f"长度 {len(TOKEN)}" in captured.out

    def test_custom_path_prefix(self, tmp_path, monkeypatch):
        _patch_runner(monkeypatch, [_entry(path="/custom/chat")])
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        out = tmp_path / "tok.json"
        assert eft.main([str(flows), "--output", str(out), "--path-prefix", "/custom"]) == 0
        assert json.loads(out.read_text())["token"] == TOKEN

    def test_multiple_tokens_takes_first_and_notes(self, tmp_path, monkeypatch, capsys):
        _patch_runner(monkeypatch, [_entry(), _entry(TOKEN_OTHER)])
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        out = tmp_path / "tok.json"
        assert eft.main([str(flows), "--output", str(out)]) == 0
        assert json.loads(out.read_text())["token"] == TOKEN
        assert "2 个不同 token" in capsys.readouterr().out


class TestErrorPaths:
    def test_flows_file_missing(self, tmp_path, capsys):
        out = tmp_path / "tok.json"
        code = eft.main([str(tmp_path / "nope.mitm"), "--output", str(out)])
        assert code == 2
        assert not out.exists()
        assert "不存在" in capsys.readouterr().err

    def test_empty_jsonl_returns_1(self, tmp_path, monkeypatch, capsys):
        _patch_runner(monkeypatch, [])  # addon 未产出任何行
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        out = tmp_path / "tok.json"
        assert eft.main([str(flows), "--output", str(out)]) == 1
        assert not out.exists()
        err = capsys.readouterr().err
        assert "未提取到" in err
        assert "/v2/chat/completions" in err

    def test_all_paths_unmatched_returns_1(self, tmp_path, monkeypatch, capsys):
        _patch_runner(monkeypatch, [_entry(path="/v1/models"), _entry(path="/billing/x")])
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        out = tmp_path / "tok.json"
        assert eft.main([str(flows), "--output", str(out)]) == 1
        assert not out.exists()

    def test_invalid_token_prefix_returns_1(self, tmp_path, monkeypatch):
        _patch_runner(monkeypatch, [_entry(token="AAAA-not-v3"), _entry(token="v3:tiny")])
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        out = tmp_path / "tok.json"
        assert eft.main([str(flows), "--output", str(out)]) == 1
        assert not out.exists()

    def test_mitmdump_missing_returns_2(self, tmp_path, monkeypatch, capsys):
        _patch_runner(monkeypatch, [], exc=FileNotFoundError("mitmdump"))
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        out = tmp_path / "tok.json"
        assert eft.main([str(flows), "--output", str(out)]) == 2
        assert "mitmdump 不在 PATH" in capsys.readouterr().err

    def test_output_argument_required(self, tmp_path):
        flows = tmp_path / "flows.mitm"
        flows.write_bytes(b"x")
        with pytest.raises(SystemExit) as exc:
            eft.main([str(flows)])
        assert exc.value.code == 2


class TestAddonGeneration:
    def test_addon_content_mentions_header_and_jsonl(self, tmp_path):
        addon = tmp_path / "addon.py"
        jsonl = tmp_path / "tokens.jsonl"
        eft._write_addon(addon, jsonl)
        content = addon.read_text(encoding="utf-8")
        assert "x-device-token" in content
        assert "x-user-id" in content
        assert str(jsonl) in content
        assert "def request(flow)" in content

    def test_save_token_permissions_and_payload(self, tmp_path):
        out = tmp_path / "nested" / "tok.json"
        eft._save_token(TOKEN, "mitm:/x.mitm", out)
        assert out.stat().st_mode & 0o777 == 0o600
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["token"] == TOKEN
        assert data["_warning"] == "per-device secret, never share"
        assert data["source"] == "mitm:/x.mitm"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
