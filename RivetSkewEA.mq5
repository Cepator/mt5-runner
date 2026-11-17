//+------------------------------------------------------------------+
//| RivetSkewEA.mq5                                                  |
//| Positive-skew weekly trend follower for the Purple demo account. |
//| Frame validated 2026-11-15 in ~/trading-backtest (walk-forward): |
//| Donchian breakout N=10 weekly bars, enter next bar open, initial |
//| stop 2x ATR(14), chandelier trail 3x ATR on weekly closes,       |
//| 0.5% fixed-fractional risk, 30:1 notional cap, 0.10R cost load.  |
//| Principle (owner 2026-11-17): many small red losses acceptable,  |
//| blue trades are trailed so they blossom into few very large wins.|
//+------------------------------------------------------------------+
#property copyright "Rivet"
#property version   "1.00"
#include <Trade/Trade.mqh>

input int    InpDonchianN = 10;    // Donchian lookback (completed weekly bars)
input int    InpATRPeriod = 14;    // ATR period (weekly)
input double InpStopATR   = 2.0;   // initial stop distance = x * ATR
input double InpTrailATR  = 3.0;   // chandelier trail distance = x * ATR
input double InpRiskPct   = 0.5;   // risk per trade, percent of equity
input double InpMaxLev    = 30.0;  // max notional / equity
input double InpMaxDDPct  = 15.0;  // halt if equity drawdown from init exceeds %
input long   InpMagic     = 20261117;

CTrade   trade;
datetime g_weekBar = 0;
double   g_initEquity = 0.0;
bool     g_halted = false;
int      g_atr = INVALID_HANDLE;

int OnInit()
{
   if((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("RivetSkew: LIVE account refused. Demo only by owner mandate.");
      return INIT_FAILED;
   }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("RivetSkew: WARN algo trading not enabled yet, waiting.");
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(25);
   g_atr = iATR(_Symbol, PERIOD_W1, InpATRPeriod);
   if(g_atr == INVALID_HANDLE) { Print("RivetSkew: ATR handle failed"); return INIT_FAILED; }
   g_initEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   g_weekBar = iTime(_Symbol, PERIOD_W1, 0);
   PrintFormat("RivetSkew init %s equity=%.2f (kill-switch %.0f, dd-halt at %.2f)",
               _Symbol, g_initEquity, InpMagic, g_initEquity*(1.0-InpMaxDDPct/100.0));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { PrintFormat("RivetSkew deinit reason=%d", reason); }

double AtrWeekly()
{
   double buf[1];
   if(CopyBuffer(g_atr, 0, 1, 1, buf) != 1) return 0.0;
   return buf[0];
}

bool HasPosition(long &type, double &openPrice, datetime &openTime, ulong &ticket)
{
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(!PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      type      = PositionGetInteger(POSITION_TYPE);
      openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      openTime  = (datetime)PositionGetInteger(POSITION_TIME);
      ticket    = tk;
      return true;
   }
   return false;
}

double LotsFor(double stopDist, double price)
{
   double tickVal = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSz  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickVal <= 0.0 || tickSz <= 0.0 || stopDist <= 0.0) return 0.0;
   double riskMoney = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPct / 100.0;
   double lossPerLot = stopDist / tickSz * tickVal;
   if(lossPerLot <= 0.0) return 0.0;
   double lots = riskMoney / lossPerLot;
   double notionalCap = AccountInfoDouble(ACCOUNT_EQUITY) * InpMaxLev;
   double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(contract * price > 0.0) lots = MathMin(lots, notionalCap / (contract * price));
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step > 0.0) lots = MathFloor(lots / step) * step;
   if(lots < vmin) return 0.0;          // never oversize to satisfy min lot
   return MathMin(lots, vmax);
}

void ManagePosition()
{
   long type; double openPrice; datetime openTime; ulong ticket;
   if(!HasPosition(type, openPrice, openTime, ticket)) return;
   double atr = AtrWeekly();
   if(atr <= 0.0) return;

   int bars = Bars(_Symbol, PERIOD_W1, openTime, TimeCurrent()) + 1;
   if(bars < 2) return;
   int need = MathMin(bars - 1, 500);
   int hiIdx = iHighest(_Symbol, PERIOD_W1, MODE_CLOSE, need, 1);
   int loIdx = iLowest(_Symbol, PERIOD_W1, MODE_CLOSE, need, 1);
   if(hiIdx < 0 || loIdx < 0) return;
   double hiClose = iClose(_Symbol, PERIOD_W1, hiIdx);
   double loClose = iClose(_Symbol, PERIOD_W1, loIdx);
   double curSL   = PositionGetDouble(POSITION_SL);
   double bid     = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask     = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   if(type == POSITION_TYPE_BUY)
   {
      double trail = MathMax(hiClose - InpTrailATR * atr, openPrice - InpStopATR * atr);
      if(trail > curSL + _Point) trade.PositionModify(ticket, NormalizeDouble(trail, _Digits), 0.0);
   }
   else
   {
      double trail = MathMin(loClose + InpTrailATR * atr, openPrice + InpStopATR * atr);
      if((curSL == 0.0 || trail < curSL - _Point) && trail > 0.0)
         trade.PositionModify(ticket, NormalizeDouble(trail, _Digits), 0.0);
   }
}

void TryEntry()
{
   long type; double openPrice; datetime openTime; ulong ticket;
   if(HasPosition(type, openPrice, openTime, ticket)) return;
   int want = InpDonchianN + 3;
   MqlRates r[];
   int got = CopyRates(_Symbol, PERIOD_W1, 0, want, r);
   if(got < want) return;                        // ascending: r[0] oldest, r[got-1] forming
   int last = got - 2;                            // last completed bar
   if(last < 1) return;
   double hh = -DBL_MAX, ll = DBL_MAX;
   for(int i = 2; i <= InpDonchianN + 1; i++)     // window EXCLUDES the breakout bar
   {
      int idx = got - 1 - i;
      if(idx < 0) return;
      hh = MathMax(hh, r[idx].high);
      ll = MathMin(ll, r[idx].low);
   }
   double atr = AtrWeekly();
   if(atr <= 0.0) return;
   double closePrev = r[last].close;
   double price = 0.0; int dir = 0;
   if(closePrev > hh) { dir =  1; price = SymbolInfoDouble(_Symbol, SYMBOL_ASK); }
   else if(closePrev < ll) { dir = -1; price = SymbolInfoDouble(_Symbol, SYMBOL_BID); }
   if(dir == 0) return;
   double stopDist = InpStopATR * atr;
   double lots = LotsFor(stopDist, price);
   if(lots <= 0.0) { PrintFormat("RivetSkew: breakout %d on %s but lot=0 (equity too small for min lot at 0.5%% risk)", dir, _Symbol); return; }
   double sl = (dir > 0) ? price - stopDist : price + stopDist;
   bool ok = (dir > 0)
      ? trade.Buy(lots, _Symbol, 0.0, NormalizeDouble(sl, _Digits), 0.0, "RivetSkew L")
      : trade.Sell(lots, _Symbol, 0.0, NormalizeDouble(sl, _Digits), 0.0, "RivetSkew S");
   PrintFormat("RivetSkew: %s %s breakout close=%.5f hh=%.5f ll=%.5f atr=%.5f lots=%.2f sl=%.5f -> %s",
               dir > 0 ? "BUY" : "SELL", _Symbol, closePrev, hh, ll, atr, lots, sl, ok ? "OK" : "FAIL");
}

void OnTick()
{
   if(g_halted) return;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_initEquity > 0.0 && eq <= g_initEquity * (1.0 - InpMaxDDPct / 100.0))
   {
      PrintFormat("RivetSkew: KILL-SWITCH equity %.2f below halt line, closing all", eq);
      for(int i = PositionsTotal()-1; i >= 0; i--)
      {
         ulong tk = PositionGetTicket(i);
         if(tk != 0 && PositionSelectByTicket(tk) && PositionGetInteger(POSITION_MAGIC) == InpMagic)
            trade.PositionClose(tk);
      }
      g_halted = true;
      Comment("RivetSkew HALTED by drawdown switch");
      return;
   }
   Comment(StringFormat("RivetSkew %s | equity %.2f | risk %.1f%% | DD-halt %.2f | N=%d ATR%d stop%.1f trail%.1f",
           _Symbol, eq, InpRiskPct, g_initEquity*(1.0-InpMaxDDPct/100.0), InpDonchianN, InpATRPeriod, InpStopATR, InpTrailATR));
   datetime w0 = iTime(_Symbol, PERIOD_W1, 0);
   ManagePosition();
   if(w0 != g_weekBar)
   {
      g_weekBar = w0;
      TryEntry();
   }
}
