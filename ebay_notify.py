"""eBay marketplace account deletion notifications.

eBay requires every production keyset to expose an HTTPS endpoint that
  * answers GET ?challenge_code=... with {"challengeResponse": sha256(challenge + token + endpoint)}, and
  * accepts POSTed deletion notices with a 2xx.
This app stores no eBay user data, so there is nothing to erase: notices are acknowledged and logged only.
Enabled by setting EBAY_VERIFICATION_TOKEN (32-80 chars: letters, digits, _ or -) to the same value entered in
the eBay developer portal.
"""
import hashlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import EBAY_VERIFICATION_TOKEN, PUBLIC_URL

log = logging.getLogger("ebay")
router = APIRouter()
PATH = "/ebay/account-deletion"


def challenge_response(challenge_code: str, token: str, endpoint: str) -> str:
    return hashlib.sha256((challenge_code + token + endpoint).encode()).hexdigest()


@router.get(PATH, include_in_schema=False)
def verify(challenge_code: str = "") -> Response:
    if not EBAY_VERIFICATION_TOKEN or not PUBLIC_URL or not challenge_code:
        return Response(status_code=404)
    digest = challenge_response(challenge_code, EBAY_VERIFICATION_TOKEN, PUBLIC_URL + PATH)
    return JSONResponse({"challengeResponse": digest})


@router.post(PATH, include_in_schema=False)
async def notification(request: Request) -> Response:
    if not EBAY_VERIFICATION_TOKEN:
        return Response(status_code=404)
    log.info("eBay account-deletion notice received (%d bytes); no eBay user data is stored", len(await request.body()))
    return Response(status_code=204)
