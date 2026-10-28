# ATrad Roadshow TMS — API Endpoints Reference

Captured 2026-09-14 via live browser session (chrome-devtools XHR capture) while placing a live order.
Base URL: `https://tms.roadshowsecurities.com.np/atsweb`
Auth account used: `TMS_ROADSHOW_USER` / `TMS_ROADSHOW_PASS` (see `~/nepse-engine/.env` — "LIVE ORDER account").

All GET endpoints are `application/x-www-form-urlencoded`, called with header `x-requested-with: XMLHttpRequest`,
and require the session cookie (`JSESSIONID`, `role=OnlineUser`, `broker_code=RSL`, `watchID=...`) obtained after login.
Most calls append `dojo.preventCache=<epoch_ms>` as a cache-buster (harmless if omitted).

## ⚠️ Known issue to resolve before automating buys

The order form's `actionSelect` param controls BUY vs SELL. During this capture, a **sell** was intended but
`actionSelect=1` produced a live **BUY** order (confirmed via `getUCCActiveBlotterData` response: `"action":"BUY"`).
**Before wiring up automated orders, verify the correct value for SELL** (likely `actionSelect=2`, but unconfirmed —
capture a manual sell in the UI and diff the POST body against the one below).

**Resolved 2026-09-16** (see `atrad_scraper.py` `ORDER_BUY`/`ORDER_SELL` comments): `actionSelect=2` is
live-confirmed correct for SELL — the 2026-09-11 SONA HARD_STOP auto-close accepted an `actionSelect=2`
submission, and `portfolio` shows that row closed the same day with `exit_reason='STOP_LOSS'` and a real
negative PnL. A mixed-up BUY would have grown the position instead of closing it, so this is real evidence,
not just an "accepted" API response.

## Auth / Session

| Action | Method | Path | Notes |
|---|---|---|---|
| Login page params | GET | `/login?action=getLoginPageParams&format=json` | called before login form renders |
| Login submit | POST | `/login` (form body, not captured this session) | standard username/password form POST |
| Session check | GET | `/login?action=checkUserSession&format=json&txtUserName=<user>` | polled every ~1s while app is open (session keepalive) |

## Home / App shell

| Action | Method | Path |
|---|---|---|
| App version | GET | `/home?action=getVersion&format=json` |
| Server time | GET | `/home?action=getJBossTime&format=json` |
| Market status (open/closed) | GET | `/home?action=marketStatus` |
| Refresh interval config | GET | `/home?action=getRefreshTime&format=json` |

## Client

| Action | Method | Path |
|---|---|---|
| Broker collateral URL | GET | `/client?action=getBrokerCollateralURL&format=json` |
| User's broker & exchange | GET | `/client?action=getUsersBrokerAndExchange&format=json&username=<user>` |

## Market / Watch

| Action | Method | Path |
|---|---|---|
| Ticker data | GET | `/market?action=getTickerData&format=json&tickerId=0` |
| Full watch list (live prices) | GET | `/watch?action=fullWatch&format=json&exchange=NEPSE&bookDefId=1&watchId=<watchId>&lastUpdatedId=<n>` — poll, `lastUpdatedId` is a cursor |
| Watch for one security | GET | `/watch?action=getWatchForSecurity&format=json&securityid=<SYM>&exchange=NEPSE&bookDefId=1` |
| All securities list | GET | `/watch?action=getAllSecurities&format=json` |
| Custom watch lists | GET | `/watch?action=getCustomWatches&format=json` |
| Sector data for watch | GET | `/watch?action=getSectorData&format=json` |
| Sector summary (index board) | GET | `/sector?action=getSectorDataAll&format=json&exchange=NEPSE&sectorId=NEPSE&sectorIdSL=SENSIND` |

## Market details (order book / stats)

| Action | Method | Path |
|---|---|---|
| Order book for a security | GET | `/marketdetails?action=getOrderBook&format=json&board=1&security=<SYM>` |
| Price impact preview | GET | `/marketdetails?action=getPriceChange&format=json&securityid=<SYM>&market=NEPSE&bookDefId=1&price=<p>&settlementDate=<M/D/YYYY>&commmision=0&side=<1|2>&cseFee=0.02&board=1` |
| Trade stats (index/turnover/volume) | GET | `/marketdetails?action=getTradeStats&format=json` |
| CSE announcement unread count | GET | `/marketdetails?action=getUnreadCSEAnnouncementCount&format=json&msgtype=NEPSE` |
| Broker announcement unread count | GET | `/marketdetails?action=getUnreadBrokerAnnouncementCount&format=json&msgtype=BROKER` |
| Active/inactive order status lists | GET | `/marketdetails?action=getActiveOrderStatuses` / `getInactiveOrderStatuses` / `getOrderStatuses` |
| CP members | GET | `/marketdetails?action=getCPMembers&format=json` |
| Debt CSE fees | GET | `/marketdetails?action=getCSEFeesForDebt&format=json` |
| Circuit filter value | GET | `/marketdetails?action=getDNALTPCircuitFilterVal&format=json` |

## Order — pre-submit checks (call these before POSTing an order)

| Action | Method | Path |
|---|---|---|
| User details | GET | `/order?action=getUserDetails&format=json` |
| Config settings | GET | `/order?action=getConfigSettings&format=json` |
| Market status for a security | GET | `/order?action=getMarketStatus&format=json&securityid=<SYM>&bordId=1&exchange=NEPSE` |
| Market details | GET | `/order?action=getMarketDetails&format=json&market=NEPSE` |
| Security properties | GET | `/order?action=getSecurityProperties&format=json&txtSecurityId=<SYM>` |
| **Order restrictions / buying power** | GET | `/order?action=getOrderRestrictions&format=json&clientAcc=<user>&exchange=NEPSE&broker=RSL&clientAnctId=<acntId>&security=<SYM>` |
| BOID user details | GET | `/order?action=getBOIDUserDetails&format=json` |
| Role permission check | GET | `/order?action=checkUserRolePermission&format=json` |
| Margin client check | GET | `/order?action=isMarginClient&format=json` |
| **Buy-disable check** (also used for sell price/qty validation) | GET | `/order?action=checkBuyDisable&format=json&txtCDSActCode=<user>&txtSecuritycode=<SYM>&exchange=NEPSE&broker=RSL&accountid=<acntId>&ordPrice=<price>&ordQty=<qty>` |
| Commission calculator | GET | `/order?action=calcCommission&format=json&orderValue=<price*qty>&accountType=1&broker=RSL&exchange=NEPSE` |

### `getOrderRestrictions` response — this is where real pre-trade limits live

```json
{
  "orderlimits": {
    "perdaylimitremaining": "9996923.5062",
    "perorderlimit": "5000080",
    "buyingpower": "3591.7",        // <-- available cash/margin to buy with, in NPR
    "availableQty": "0",             // <-- shares held & sellable (for sell-side checks)
    "pendingBuyQty": "0",
    "isBuyAllowed": "true",
    "isSellAllowed": "true",
    "isMargin": "false",
    "security": "AKPL",
    "assetType": "EQUITY"
  }
}
```

Use `buyingpower` client-side to pre-validate a BUY before submitting (price × qty must be ≤ buyingpower),
and `availableQty` for a SELL (qty must be ≤ availableQty, accounting for T+2 settlement — unsettled shares
from a recent buy won't show here yet). `checkBuyDisable` does NOT check funds — it returned `buyDisable:false`
even for an order that exceeded buying power; it only flags security-level trading halts. **Funds sufficiency
is only actually enforced server-side at order submission time (see error codes below).**

**Note (2026-09-16):** `availableQty` here is ATrad's own real-time sellable-quantity figure — a candidate
source of truth for settlement status that's more immediate than the daily `meroshare.py` sync (which is
what `portfolio.demat_confirmed_date` currently relies on, see execution_monitor.py's T+3 settlement gate).
Not wired up yet — flagging as a possible future improvement, not implemented.

## Order — submit

| Action | Method | Path | Body |
|---|---|---|---|
| **Submit order** | **POST** | `/order` | see below |

Captured POST body (this instance produced a BUY, not the intended SELL — see warning above):

```
action=submitOrder
&market=NEPSE
&broker=RSL
&format=json
&clientOrderId=
&cseOrderId=
&brokerClient=1
&orderStatus=Open
&filledQty=
&acntid=<acntId>              # e.g. 7196
&oldPrice=
&oldQty=
&remainder=
&orderplacedate=
&marketPrice=<current LTP>    # informational, e.g. 307
&oldDisclose=
&txtContraBroker=110
&txtapprovalReason=
&txtsenttoapproval=no
&txtCompId=
&txtOdrStatus=
&duplicateOrderId=<random idempotency token, e.g. 26AAJhINzV>
&product=web
&clientAcc=<user>%20(%20<Full Name>)   # e.g. BS92009 ( BASANTA SHRESTHA-79298)
&assetSelect=1                 # 1 = Equity (unconfirmed alt values)
&actionSelect=1                # 1 = BUY (confirmed via blotter). SELL value NOT YET CONFIRMED — verify before automating
&txtSecurity=<SYM>             # e.g. PHCL
&cmbTypeOfOrder=1              # 1 = Regular
&spnQuantity=<qty>
&spnPrice=<price>
&cmbTif=16                     # time-in-force code
&cmbTifDays=1
&cmbBoard=1
&hiddenSpnCseFee=0.02
&txtContraBroker_=110
&brokerClientVal=1
&confirm=1
```

Success response: `{"code":"0", "description":"javascriptOrderSuccessesFullySubmitted", "data":"success"}`

### Error responses and how to decode them

Failed submits return `{"code":"<numeric>", "description":"javascript<numeric>", "data":"javascript<numeric>"}` —
the `description`/`data` are NOT human-readable; they're message-catalog **keys**, not the message itself.

Tested live: submitted AKPL BUY, 100 @ 250 (order value NPR 25,000) against an account with `buyingpower`
of only NPR 3,591.70 (confirmed via `getOrderRestrictions` beforehand). Expected an insufficient-funds
rejection; got instead:

```json
{"code":"3907", "description":"javascript3907", "data":"javascript3907"}
```

Resolved via the in-page translator (loaded by `<script src="language/scripts/ClientMsgTranslator.jsp">`,
instantiated as `var language = new ATRAD.ClientMsgTranslator();` — call `language.getString('javascript3907')`
in-page, or fetch `language/scripts/ClientMsgTranslator.jsp` directly for the full code→message map):

> **code 3907 = "Price you entered is not within valid range"**

i.e. AKPL's circuit/DPR band at the time rejected price=250 outright — the order never got far enough to
hit a funds check.

**Follow-up test (same session, price moved inside range):** with AKPL's live order book at bid 255.2 /
ask 256.9, resubmitted BUY 50 @ 257 (order value NPR 12,850) against the same NPR 3,591.70 buying power.
This cleared the price check and hit the actual funds check:

```json
{"code":"3050", "description":"javascript3050", "data":"javascript3050"}
```

> **code 3050 = "Buying power is insufficient"**

So confirmed: **3907 = bad price, 3050 = insufficient funds** — two distinct, now-mapped codes. General
pattern for automation: on any submit response with `code != "0"`, immediately call
`language.getString('javascript<code>')` (or replicate its lookup table) to get the real reason before
deciding how to react — do not assume a non-zero code means any specific failure type, and don't assume
`getOrderRestrictions.buyingpower` alone is sufficient pre-validation (price-range and funds are separate,
sequential server-side checks — a bad price masks a funds problem until price is fixed).

## Order — status / blotter polling (after submit)

| Action | Method | Path |
|---|---|---|
| Incremental blotter updates | GET | `/order?action=getUpdatedUCCBlotterData&format=json&lastBlotterId=<cursor>` — poll every ~1s |
| Active blotter (all open orders) | GET | `/order?action=getUCCActiveBlotterData&format=json&clientAcc=ALL&securityId=ALL&exchange=all&ordStatus=ALL&ordType=ALL&lstUpdateTime=&assetClass=ALL` |

Example blotter row from a live order:

```json
{
  "clientorderid": "73623500",
  "securitycode": "PHCL",
  "action": "BUY",
  "orderStatus": "NEW",
  "orderQuantity": "10",
  "filledquantity": "0",
  "orderprice": "306.5",
  "clientaccountcode": "BS92009",
  "timeinforce": "DAY",
  "orderplacedate": "2026-09-14 13:27:50",
  "exchangeid": "NEPSE",
  "exchangeorderid": "2026091405020836",
  "typeOfOrder": "REGULAR"
}
```

## Notes for future automation work

- Session keepalive: the UI polls `login?action=checkUserSession` and `home?action=marketStatus` every ~1s;
  a background automation script should replicate this or the session may expire.
- `watch?action=fullWatch` uses a `lastUpdatedId` cursor for incremental diffs — reuse the pattern for a lightweight poller.
- Order submission is a single unauthenticated-by-CSRF-token POST relying purely on session cookie + `duplicateOrderId`
  as an idempotency key — generate a fresh random token per order to avoid dedupe rejection.
- **Do not automate live BUY/SELL submission without first confirming the `actionSelect` value for SELL** and
  testing on a small order size, given the account is a live-money account (not sandbox).
- Session died twice this session after only ~1-2 minutes of pure-fetch calls (no UI open / no heartbeat
  polling running) — confirmed via `login?action=checkUserSession` returning `{"validation":[false]}`, and
  further calls silently 200-ing back the **login page HTML** instead of JSON (no error thrown — check
  `Content-Type` or sniff for `<!DOCTYPE html>` in the response body, not just HTTP status, to detect this).
  An automation client must run the `checkUserSession`/`marketStatus` heartbeat on the same ~1s cadence the
  UI does, or it will silently start getting HTML back mid-session.
- Pre-trade check order for a BUY: `getOrderRestrictions` (get `buyingpower`) → client-side validate
  price×qty ≤ buyingpower → `checkBuyDisable` (security-level halt check only, not funds) → `calcCommission`
  (display only) → submit. Still confirm server-side funds rejection behavior once a genuine insufficient-funds
  case is captured (today's test hit the price-range check first, not funds).
