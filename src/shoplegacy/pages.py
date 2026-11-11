"""HTML pages served from ``static/``."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import ANON, PRINCIPAL
from shoplegacy.observability import CSP_NONCE_PLACEHOLDER

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]

_NOT_FOUND_HTML = (
    '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>가게유산</title><link rel="stylesheet" href="/static/tokens.css">'
    '<link rel="stylesheet" href="/static/style.css"></head>'
    '<body data-chrome="none"><main class="wrap"><h1>가게유산</h1>'
    '<p>찾으시는 페이지가 없습니다.</p><p><a href="/">처음 화면으로</a></p>'
    '<p><a id="cards-link" class="btn" href="/cards">공개 카드 보기</a></p>'
    "</main></body></html>"
)

_LOGIN_HTML = (
    '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>가게유산 로그인</title><link rel="stylesheet" href="/static/style.css"></head>'
    '<body><main class="wrap"><h1>가게유산</h1><p>로그인</p></main></body></html>'
)


def _page(name: str, request: Request) -> HTMLResponse:
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    nonce = getattr(request.state, "csp_nonce", "")
    return HTMLResponse(
        html.replace(CSP_NONCE_PLACEHOLDER, nonce), headers={"Cache-Control": "no-store"}
    )


def _not_found() -> HTMLResponse:
    return HTMLResponse(_NOT_FOUND_HTML, status_code=404)


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _page("index.html", request)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if (STATIC_DIR / "login.html").is_file():
        return _page("login.html", request)
    return HTMLResponse(_LOGIN_HTML)


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request) -> HTMLResponse:
    return _page("onboarding.html", request)


@router.get("/interview/{session_id}", response_class=HTMLResponse)
def interview_page(session_id: str, request: Request) -> HTMLResponse:
    return _page("interview.html", request)


@router.get("/shops/{shop_id}/consent", response_class=HTMLResponse)
def consent_page(shop_id: str, request: Request) -> HTMLResponse:
    return _page("consent.html", request)


@router.get("/review", response_class=HTMLResponse)
def inbox_page(request: Request) -> HTMLResponse:
    return _page("inbox.html", request)


@router.get("/review/{card_id}", response_class=HTMLResponse)
def review_page(card_id: str, request: Request) -> HTMLResponse:
    return _page("review.html", request)


@router.get("/cards", response_class=HTMLResponse)
def cards_page(request: Request) -> HTMLResponse:
    return _page("cards.html", request)


@router.get("/cards/{card_id}", response_class=HTMLResponse)
def card_page(card_id: str, request: Request, state: StateDep) -> Response:
    # Checked as an anonymous visitor, so only published cards exist here.
    token = PRINCIPAL.set(ANON)
    try:
        row = state.store.get_card(card_id)
    finally:
        PRINCIPAL.reset(token)
    if row is None or row.status != "published":
        return _not_found()
    return _page("card.html", request)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    return _page("dashboard.html", request)


@router.get("/members", response_class=HTMLResponse)
def members_page(request: Request) -> HTMLResponse:
    return _page("members.html", request)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return _page("settings.html", request)


@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request) -> HTMLResponse:
    return _page("billing.html", request)


@router.get("/billing/pay-test", response_class=HTMLResponse)
def pay_test_page(request: Request) -> HTMLResponse:
    return _page("pay_test.html", request)


@router.get("/owner/{token}", response_class=HTMLResponse)
def owner_page(token: str, request: Request) -> HTMLResponse:
    # The token is checked by the page script through the API.
    return _page("owner.html", request)
