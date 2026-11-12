"""
봇 실행 결과(state/status/log/plan)를 저장소에 안전하게 저장한다.
여러 실행이 동시에 끝나도 기록이 사라지지 않도록:
  - state.json / status.json : 이번 실행 결과가 최신이므로 이번 것으로
  - log.md                   : 원격 기록 + 이번 실행에서 새로 생긴 줄을 합침
  - plan.json                : 그 사이 앱에서 바뀌었으면 앱 쪽(원격)을, 아니면 이번 실행 결과를
사용법: python save_records.py "커밋 메시지"
"""
import subprocess
import sys
import time

FILES = ["state.json", "status.json", "log.md", "plan.json", "market.json", "answers.json", "state_archive.json"]


def sh(*a, check=True):
    return subprocess.run(a, check=check, capture_output=True, text=True)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def show(ref, path):
    r = sh("git", "show", f"{ref}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "매매 기록"
    base = sh("git", "rev-parse", "HEAD").stdout.strip()          # 실행 시작 시점
    mine = {f: read(f) for f in FILES}                             # 이번 실행 결과
    orig = {f: show(base, f) for f in FILES}
    for attempt in range(6):
        sh("git", "fetch", "-q", "origin", "main")
        remote = {f: show("origin/main", f) for f in FILES}
        sh("git", "reset", "-q", "--hard", "origin/main")
        out = {}
        out["state.json"] = mine["state.json"]
        out["status.json"] = mine["status.json"]
        out["market.json"] = mine["market.json"] or remote["market.json"]
        # 로그: 원격 + 이번에 새로 추가된 줄
        r_log, m_log, o_log = remote["log.md"] or "", mine["log.md"] or "", orig["log.md"] or ""
        new_lines = m_log[len(o_log):] if m_log.startswith(o_log) else ""
        out["log.md"] = r_log + ("" if not new_lines or r_log.endswith("\n") or not r_log else "\n") + new_lines
        # 계획: 앱이 그 사이 바꿨으면 앱 것을 존중
        out["plan.json"] = mine["plan.json"] if remote["plan.json"] == orig["plan.json"] else remote["plan.json"]
        out["answers.json"] = mine["answers.json"] if remote["answers.json"] == orig["answers.json"] else remote["answers.json"]
        # 이번 실행이 건드리지 않은 파일은 원격(더 최신) 그대로 둔다 (예: 질문 답변 실행이 매매 기록을 덮지 않게)
        for f in FILES:
            if mine[f] == orig[f]:
                out[f] = remote[f]
        changed = False
        for f, text in out.items():
            if text is None:
                continue
            if remote[f] != text:  # 원격과 다르면 저장 (새 파일은 reset 후에도 남아 있으므로 원격 기준으로 비교)
                with open(f, "w", encoding="utf-8") as fp:
                    fp.write(text)
                sh("git", "add", f)
                changed = True
        if not changed:
            print("저장할 변경 없음")
            return
        sh("git", "commit", "-q", "-m", msg)
        if sh("git", "push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            print(f"기록 저장 완료 (시도 {attempt + 1})")
            return
        time.sleep(2 + attempt * 2)
    print("기록 저장 실패: 동시 저장이 계속 겹침")
    sys.exit(1)


if __name__ == "__main__":
    main()
