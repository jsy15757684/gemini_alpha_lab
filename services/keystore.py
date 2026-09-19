"""빗썸 API 키 보관.

우선순위: 환경변수 > 디스크 파일.

환경변수(BITHUMB_API_KEY / BITHUMB_SECRET_KEY)를 권장한다.
  · 재배포·재시작에도 살아남는다
  · 디스크에 평문으로 남지 않는다
디스크 저장(data/broker_keys.json)은 편의를 위한 대체 경로이며 **평문**이다.
UI 문구도 그렇게 표기해야 한다 — 암호화한다고 적으면 거짓이 된다.
"""

import os
import json
import logging
from typing import Any, Dict, Optional

from services.bithumb import BithumbAccount

logger = logging.getLogger(__name__)

KEYS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bithumb_key.json")

ENV_API_KEY = "BITHUMB_API_KEY"
ENV_SECRET_KEY = "BITHUMB_SECRET_KEY"


class KeyStore:
    def __init__(self):
        self.source = "none"          # "env" | "disk" | "none"
        self.account = BithumbAccount()
        self._load()

    def _load(self):
        api = (os.getenv(ENV_API_KEY) or "").strip()
        sec = (os.getenv(ENV_SECRET_KEY) or "").strip()
        if api and sec:
            self.account = BithumbAccount(api, sec)
            self.source = "env"
            logger.info(f"빗썸 API 키를 환경변수({ENV_API_KEY})에서 로드했습니다.")
            return

        try:
            if os.path.exists(KEYS_FILE):
                with open(KEYS_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                api, sec = (d.get("apiKey") or "").strip(), (d.get("secretKey") or "").strip()
                if api and sec:
                    self.account = BithumbAccount(api, sec)
                    self.source = "disk"
                    logger.info("빗썸 API 키를 저장 파일에서 로드했습니다 (평문 저장).")
        except Exception as e:
            logger.warning(f"저장된 키를 읽지 못했습니다: {e}")

    def save(self, api_key: str, secret_key: str) -> None:
        """디스크에 평문 저장. 환경변수로 주입된 경우에는 그쪽이 계속 우선한다."""
        if self.source == "env":
            raise PermissionError(
                f"키가 환경변수({ENV_API_KEY})로 주입되어 있어 화면에서 변경할 수 없습니다. "
                f"배포 환경의 환경변수를 수정하세요.")
        os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump({"apiKey": api_key, "secretKey": secret_key}, f)
        self.account = BithumbAccount(api_key, secret_key)
        self.source = "disk"

    def clear(self) -> None:
        if self.source == "env":
            raise PermissionError(
                f"환경변수({ENV_API_KEY})로 주입된 키는 화면에서 해제할 수 없습니다. "
                f"배포 환경의 환경변수를 삭제하세요.")
        try:
            if os.path.exists(KEYS_FILE):
                os.remove(KEYS_FILE)
        except Exception as e:
            logger.warning(f"키 파일 삭제 실패: {e}")
        self.account = BithumbAccount()
        self.source = "none"

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.account.configured,
            "maskedKey": self.account.masked_key(),
            "source": self.source,
            "editable": self.source != "env",
            "storageNote": ("환경변수로 주입된 키입니다 (디스크에 저장되지 않음)."
                            if self.source == "env" else
                            "서버 파일에 평문 저장됩니다. 재배포 시 사라지므로 "
                            "영구 보관은 환경변수를 사용하세요."),
        }


keystore = KeyStore()


# ───────────────────────── 나무증권 계정 보관 ─────────────────────────

NAMUH_KEYS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "namuh_key.json")

ENV_NAMUH_APP_KEY = "NAMUH_APP_KEY"
ENV_NAMUH_APP_SECRET = "NAMUH_APP_SECRET"
ENV_NAMUH_ACCOUNT_NO = "NAMUH_ACCOUNT_NO"


class NamuhKeyStore:
    def __init__(self):
        self.source = "none"          # "env" | "disk" | "none"
        from services.namuh import NamuhAccount
        self.account = NamuhAccount()
        self._load()

    def _load(self):
        from services.namuh import NamuhAccount
        app_key = (os.getenv(ENV_NAMUH_APP_KEY) or "").strip()
        app_secret = (os.getenv(ENV_NAMUH_APP_SECRET) or "").strip()
        account_no = (os.getenv(ENV_NAMUH_ACCOUNT_NO) or "").strip()
        if app_key and app_secret:
            self.account = NamuhAccount(app_key, app_secret, account_no)
            self.source = "env"
            logger.info("나무증권 API 키를 환경변수에서 로드했습니다.")
            return

        try:
            if os.path.exists(NAMUH_KEYS_FILE):
                with open(NAMUH_KEYS_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                app_key = (d.get("appKey") or "").strip()
                app_secret = (d.get("appSecret") or "").strip()
                account_no = (d.get("accountNo") or "").strip()
                if app_key and app_secret:
                    self.account = NamuhAccount(app_key, app_secret, account_no)
                    self.source = "disk"
                    logger.info("나무증권 API 키를 저장 파일에서 로드했습니다 (평문 저장).")
        except Exception as e:
            logger.warning(f"나무증권 저장된 키를 읽지 못했습니다: {e}")

    def save(self, app_key: str, app_secret: str, account_no: str) -> None:
        """디스크에 평문 저장."""
        from services.namuh import NamuhAccount
        if self.source == "env":
            raise PermissionError("키가 환경변수로 주입되어 있어 화면에서 변경할 수 없습니다.")
        os.makedirs(os.path.dirname(NAMUH_KEYS_FILE), exist_ok=True)
        with open(NAMUH_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump({"appKey": app_key, "appSecret": app_secret, "accountNo": account_no}, f)
        self.account = NamuhAccount(app_key, app_secret, account_no)
        self.source = "disk"

    def clear(self) -> None:
        from services.namuh import NamuhAccount
        if self.source == "env":
            raise PermissionError("환경변수로 주입된 키는 화면에서 해제할 수 없습니다.")
        try:
            if os.path.exists(NAMUH_KEYS_FILE):
                os.remove(NAMUH_KEYS_FILE)
        except Exception as e:
            logger.warning(f"나무증권 키 파일 삭제 실패: {e}")
        self.account = NamuhAccount()
        self.source = "none"

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.account.configured,
            "maskedKey": self.account.masked_key(),
            "maskedAccount": self.account.masked_account(),
            "source": self.source,
            "editable": self.source != "env",
            "storageNote": ("환경변수로 주입된 키입니다 (디스크에 저장되지 않음)."
                            if self.source == "env" else
                            "서버 파일에 평문 저장됩니다. 재배포 시 사라지므로 "
                            "영구 보관은 환경변수를 사용하세요."),
        }


namuh_keystore = NamuhKeyStore()

