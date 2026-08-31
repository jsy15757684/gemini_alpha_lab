#!/bin/bash
# .env 파서 — run.sh 와 systemd 서비스가 '같은 규칙' 을 쓰도록 한 곳에 둔다.
#
# systemd 의 EnvironmentFile 은 따옴표·이스케이프 처리 규칙이 이것과 달라서,
# 같은 .env 를 로컬과 서버가 다르게 읽을 수 있다. 그 차이로 디버깅이 어려워지는
# 것을 막기 위해 서비스도 이 파서를 거치게 한다.
#
# 규칙
#   · 빈 줄과 '#' 로 시작하는 줄은 무시
#   · KEY=VALUE 에서 앞뒤 공백 제거 ("KEY= value" 형태를 흔히 실수한다)
#   · 값을 감싼 따옴표 한 겹 제거
#   · 인라인 주석은 지우지 않는다 — 비밀번호에 '#' 이 들어갈 수 있다
#   · 값을 실행하지 않는다 (source 와 달리 안전하다)
#   · 빈 값은 건너뛴다

load_env_file() {
    local file="$1" line key val loaded=0 skipped=0
    [ -f "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in ''|'#'*) continue;; esac
        case "$line" in *=*) ;; *) continue;; esac

        key="${line%%=*}"
        val="${line#*=}"
        key="$(printf '%s' "$key" | tr -d '[:space:]')"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac

        [ -z "$key" ] && continue
        if [ -z "$val" ]; then skipped=$((skipped+1)); continue; fi
        export "$key=$val"
        loaded=$((loaded+1))
    done < "$file"
    ENV_LOADED=$loaded
    ENV_SKIPPED=$skipped
    return 0
}
