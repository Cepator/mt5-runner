#!/usr/bin/env python3
"""RivetSkew weekly trend follower - official MetaTrader5 Python package edition.

Replaces the terminal EA (RivetSkewEA.ex5, banked for a future local host) per
owner directive 14:45 2026-11-17: full programmatic trading, zero UI.
Frame (validated 2026-11-15 walk-forward, ~/trading-backtest):
  Donchian breakout N=10 completed weekly bars, enter at market on signal,
  initial stop 2x ATR(14, Wilder), chandelier trail 3x ATR on weekly closes,
  0.5% fixed-fractional risk, 30:1 notional cap, one position per symbol,
  demo accounts only (hard gate, abort on live).
Run modes (workflow sets ENTRIES):
  ENTRIES=0  manage trailing stops only        (daily 06:05 UTC)
  ENTRIES=1  new-week entry evaluation + trail (Sunday 22:05 UTC, dispatches)
Exit codes: 0 ok (incl. market-closed no-ops), 2 demo gate or connect failure.
"""
import sys
import os
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

MAGIC = 20261117
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
DONCHIAN_N = 10
ATR_PERIOD = 14
STOP_ATR = 2.0
TRAIL_ATR = 3.0
RISK_PCT = 0.5
MAX_LEV = 30.0
MAX_DD_PCT = 15.0
DEVIATION = 25
LOG = []
MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {msg}"
    print(line, flush=True)
    LOG.append(line)

def bail(code, msg):
    log(f"FATAL {msg}")
    sys.exit(code)

def connect_mt5():
    """Initialize with explicit terminal path + login credentials, retrying
    while a freshly installed terminal finishes its first boot: the known
    failure is (-10005, 'IPC timeout') when python connects before the
    terminal is fully up (MQL5 forum 447937 pattern)."""
    login = os.environ.get("MT5_DEMO_LOGIN")
    password = os.environ.get("MT5_DEMO_PASSWORD")
    server = os.environ.get("MT5_DEMO_SERVER")
    last_err = None
    for attempt in range(1, 7):
        kwargs = {"path": MT5_PATH}
        if login and password and server:
            kwargs.update(login=int(login), password=password, server=server)
        if mt5.initialize(**kwargs):
            log(f"mt5.initialize OK on attempt {attempt}, terminal build={mt5.version()}")
            return
        last_err = mt5.last_error()
        log(f"mt5.initialize attempt {attempt}/6 failed, last_error={last_err}")
        mt5.shutdown()
        time.sleep(15)
    bail(2, f"mt5.initialize failed after 6 attempts, last_error={last_err}")

def ensure_algo_allowed():
    """Fresh terminals boot with algo trading OFF; orders then fail with
    retcode 10027. Toggle Ctrl+E on the terminal window (focused, not blind)
    and verify via terminal_info().trade_allowed - deterministic, checked."""
    for attempt in range(1, 4):
        term = mt5.terminal_info()
        if term is not None and term.trade_allowed:
            log(f"algo trading allowed (checked attempt {attempt})")
            return
        log(f"trade_allowed=False (attempt {attempt}), focusing terminal window + Ctrl+E")
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hits = []
            def cb(hwnd, _):
                n = user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if "MetaTrader 5" in buf.value:
                        hits.append((hwnd, buf.value))
                return 1
            proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(cb)
            user32.EnumWindows(proc, 0)
            if hits:
                hwnd, title = hits[0]
                log(f"terminal window {hwnd:#x} title={title!r}")
                user32.keybd_event(0x12, 0, 0, 0)  # ALT down loosens the foreground lock
                user32.SetForegroundWindow(hwnd)
                user32.keybd_event(0x12, 0, 2, 0)  # ALT up
                time.sleep(0.5)
                user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
                time.sleep(0.05)
                user32.keybd_event(0x45, 0, 0, 0)  # E down
                time.sleep(0.05)
                user32.keybd_event(0x45, 0, 2, 0)  # E up
                user32.keybd_event(0x11, 0, 2, 0)  # Ctrl up
            else:
                log("WARN no MetaTrader 5 top-level window found for focus")
        except Exception as exc:
            log(f"WARN algo toggle error: {exc}")
        time.sleep(5)
    term = mt5.terminal_info()
    if term is None or not term.trade_allowed:
        bail(2, "algo trading still disabled after 3 toggle attempts; "
                "orders/trails would be rejected (10027)")
    log("algo trading enabled after toggle")

def atr_wilder(rates, period=ATR_PERIOD):
    trs = []
    for i in range(1, len(rates)):
        h, l, pc = rates[i].high, rates[i].low, rates[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def lots_for(symbol, stop_dist, price, equity, info):
    tick_val = info.trade_tick_value
    tick_sz = info.trade_tick_size
    if tick_val <= 0 or tick_sz <= 0 or stop_dist <= 0:
        return 0.0
    risk_money = equity * RISK_PCT / 100.0
    loss_per_lot = stop_dist / tick_sz * tick_val
    if loss_per_lot <= 0:
        return 0.0
    lots = risk_money / loss_per_lot
    contract = info.trade_contract_size
    if contract * price > 0:
        lots = min(lots, (equity * MAX_LEV) / (contract * price))
    step = info.volume_step
    vmin, vmax = info.volume_min, info.volume_max
    if step > 0:
        lots = (lots // step) * step
    if lots < vmin:
        return 0.0  # never oversize to satisfy min lot
    return min(lots, vmax)

def send_order(sym, order_type, lots, price, sl, info):
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": lots,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": 0.0,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": "RivetSkew py",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res is not None and res.retcode == 10030:  # unsupported filling -> FOK
        req["type_filling"] = mt5.ORDER_FILLING_FOK
        res = mt5.order_send(req)
    if res is None:
        log(f"{sym} order_send returned None rc={mt5.last_error()}")
        return None
    log(f"{sym} order_send retcode={res.retcode} comment={res.comment!r} deal={res.deal} order={res.order}")
    return res

def main():
    entries_on = sys.argv[1] == "1" if len(sys.argv) > 1 else True
    log(f"RivetSkew python bot start, entries={'ON' if entries_on else 'OFF (trail-only)'}")

    connect_mt5()
    ensure_algo_allowed()

    acc = mt5.account_info()
    for _ in range(4):
        if acc is not None:
            break
        log("account_info None, waiting for login to complete")
        time.sleep(5)
        acc = mt5.account_info()
    if acc is None:
        bail(2, f"account_info None after initialize, last_error={mt5.last_error()}")
    log(f"ACCOUNT login={acc.login} server={acc.server} name={acc.name!r} "
        f"trade_mode={acc.trade_mode} leverage=1:{acc.leverage} equity={acc.equity:.2f} "
        f"balance={acc.balance:.2f} margin_free={acc.margin_free:.2f}")
    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        bail(2, f"LIVE account refused. Demo only by owner mandate (trade_mode={acc.trade_mode}).")
    log("demo account confirmed, trade allowed")

    init_equity = acc.equity

    if not mt5.symbol_select(SYMBOLS[0], True):
        log(f"WARN symbol_select {SYMBOLS[0]} failed")
    for s in SYMBOLS:
        if not mt5.symbol_select(s, True):
            log(f"WARN symbol_select {s} failed")
    log("symbols in Market Watch: " + ", ".join(SYMBOLS))

    terminal = mt5.terminal_info()
    log(f"TERMINAL connected={terminal.trade_allowed} algo_button={terminal.trade_allowed} "
        f"pdf={terminal.name} {terminal.build}")

    # kill-switch sweep first (equity drawdown halt, EA frame parity)
    positions = mt5.positions_get()
    if positions is None:
        positions = []
    equity_now = mt5.account_info().equity
    if init_equity > 0 and equity_now <= init_equity * (1.0 - MAX_DD_PCT / 100.0):
        log(f"KILL-SWITCH equity {equity_now:.2f} below halt line, closing all magic positions")
        for p in positions:
            if p.magic != MAGIC:
                continue
            tick = mt5.symbol_info_tick(p.symbol)
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            res = send_order(p.symbol, close_type, p.volume, tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask, 0.0, mt5.symbol_info(p.symbol))
            if res and res.retcode not in (10009, mt5.TRADE_RETCODE_DONE):
                log(f"{p.symbol} kill-switch close FAILED rc={res.retcode}")
        log("kill-switch done, halting")
        mt5.shutdown()
        sys.exit(0)

    # manage: chandelier trail on open magic positions
    for p in positions:
        if p.magic != MAGIC:
            continue
        sym = p.symbol
        info = mt5.symbol_info(sym)
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_W1, 0, 60)
        if rates is None or len(rates) < ATR_PERIOD + 3:
            log(f"{sym} trail skip: insufficient W1 rates")
            continue
        atr = atr_wilder(rates)
        if not atr or atr <= 0:
            log(f"{sym} trail skip: ATR unavailable")
            continue
        bars_since = [r for r in rates if r.time >= p.time]
        if len(bars_since) < 2:
            log(f"{sym} trail skip: position opened inside current weekly bar, need closes")
            continue
        closes = [r.close for r in bars_since][:-1]  # completed bars only
        need = closes[:500]
        point = info.point
        if p.type == mt5.ORDER_TYPE_BUY:
            hi_close = max(need)
            trail = max(hi_close - TRAIL_ATR * atr, p.price_open - STOP_ATR * atr)
            if trail > p.sl + point:
                req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": sym,
                       "sl": trail, "tp": p.tp, "magic": MAGIC}
                res = mt5.order_send(req)
                ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
                log(f"{sym} BUY trail {p.sl} -> {trail:.5f} (hi_close {hi_close:.5f} atr {atr:.5f}) "
                    f"retcode={getattr(res, 'retcode', 'None')} ok={ok}")
            else:
                log(f"{sym} BUY trail unchanged sl={p.sl} candidate={trail:.5f}")
        else:
            lo_close = min(need)
            trail = min(lo_close + TRAIL_ATR * atr, p.price_open + STOP_ATR * atr)
            if (p.sl == 0.0 or trail < p.sl - point) and trail > 0:
                req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": sym,
                       "sl": trail, "tp": p.tp, "magic": MAGIC}
                res = mt5.order_send(req)
                ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
                log(f"{sym} SELL trail {p.sl} -> {trail:.5f} (lo_close {lo_close:.5f} atr {atr:.5f}) "
                    f"retcode={getattr(res, 'retcode', 'None')} ok={ok}")
            else:
                log(f"{sym} SELL trail unchanged sl={p.sl} candidate={trail:.5f}")

    # entries: new-week breakout evaluation
    if entries_on:
        log("ENTRY evaluation pass")
        for sym in SYMBOLS:
            info = mt5.symbol_info(sym)
            if info is None:
                log(f"{sym} entry skip: no symbol_info (not on this broker?)")
                continue
            tick = mt5.symbol_info_tick(sym)
            if tick is None or tick.bid <= 0:
                log(f"{sym} entry skip: no tick (market closed or symbol missing)")
                continue
            rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_W1, 0, 60)
            if rates is None or len(rates) < DONCHIAN_N + 4:
                log(f"{sym} entry skip: insufficient W1 rates ({0 if rates is None else len(rates)})")
                continue
            atr = atr_wilder(rates)
            if not atr or atr <= 0:
                log(f"{sym} entry skip: ATR unavailable")
                continue
            # forming bar = rates[-1]; last completed = rates[-2]; window EXCLUDES breakout bar
            forming = rates[-1]
            prev_close = rates[-2].close
            window = rates[-(DONCHIAN_N + 2):-2]
            hh = max(r.high for r in window)
            ll = min(r.low for r in window)
            age_h = (time.time() - forming.time) / 3600.0
            log(f"{sym} eval close={prev_close:.5f} hh={hh:.5f} ll={ll:.5f} atr={atr:.5f} "
                f"forming_bar_age={age_h:.1f}h")
            have = [p for p in (mt5.positions_get(symbol=sym) or []) if p.magic == MAGIC]
            if have:
                log(f"{sym} entry skip: position exists ticket={have[0].ticket}")
                continue
            if age_h > 30.0:
                log(f"{sym} entry skip: not the new-week bar (age {age_h:.1f}h)")
                continue
            direction = 0
            if prev_close > hh:
                direction = 1
            elif prev_close < ll:
                direction = -1
            if direction == 0:
                log(f"{sym} no breakout")
                continue
            stop_dist = STOP_ATR * atr
            price = tick.ask if direction == 1 else tick.bid
            lots = lots_for(sym, stop_dist, price, equity_now, info)
            if lots <= 0:
                log(f"{sym} breakout {direction} but lot=0 (equity too small for min lot at {RISK_PCT}% risk)")
                continue
            sl = price - stop_dist if direction == 1 else price + stop_dist
            sl = round(sl, info.digits)
            otype = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL
            log(f"{sym} {'BUY' if direction == 1 else 'SELL'} breakout close={prev_close:.5f} "
                f"hh={hh:.5f} ll={ll:.5f} atr={atr:.5f} lots={lots} sl={sl:.5f} -> sending")
            send_order(sym, otype, lots, price, sl, info)
    else:
        log("entries OFF, trail management only")

    acc2 = mt5.account_info()
    log(f"FINAL ACCOUNT login={acc2.login} equity={acc2.equity:.2f} balance={acc2.balance:.2f} "
        f"open_positions={len(mt5.positions_get() or [])}")
    log("RIVETSKEW_PY_OK")
    mt5.shutdown()
    sys.exit(0)

if __name__ == "__main__":
    main()
