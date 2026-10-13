//+------------------------------------------------------------------+
//|  ExportBarsCsv.mq5  -  EA that dumps the chart symbol/TF to CSV   |
//|                                                                    |
//|  RUN IT IN THE STRATEGY TESTER (not on a live chart): the live    |
//|  broker's live feed is shallow, but the tester has the deep 2016+ history.  |
//|  Tester: select this EA, @EP, M5, dates 2016.01.01 -> today,      |
//|  model "1 minute OHLC" (fast, no trading), Start. At the end it   |
//|  writes  Common\Files\<sym>_<tf>.csv  and prints the row count.   |
//|                                                                    |
//|  Format: unix time (server), open, high, low, close, tickvol,     |
//|  realvol  - clean OHLCV for Python research.                      |
//+------------------------------------------------------------------+
#property copyright "Bartek"
#property version   "2.00"

input datetime InpFrom = D'2016.01.01 00:00';

bool g_done = false;

int  OnInit() { return INIT_SUCCEEDED; }
void OnTick() { }

// fires at the end of a tester run (and on chart-removal) -> export once
double OnTester()               { ExportBars(); return 0.0; }
void   OnDeinit(const int r)    { ExportBars(); }

void ExportBars()
{
   if(g_done) return;

   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int n = CopyRates(_Symbol, PERIOD_CURRENT, InpFrom, TimeCurrent(), rates);
   if(n <= 0)
   {
      PrintFormat("ExportBarsCsv: CopyRates failed (%d) - widen the tester date range", GetLastError());
      return;
   }

   string sym = _Symbol;
   StringReplace(sym, "@", "");
   StringReplace(sym, ".", "");
   string fn = sym + "_" + EnumToString(PERIOD_CURRENT) + ".csv";

   // FILE_COMMON -> ...\MetaQuotes\Terminal\Common\Files\ (single known location)
   int h = FileOpen(fn, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
   if(h == INVALID_HANDLE)
   {
      PrintFormat("ExportBarsCsv: FileOpen failed (%d)", GetLastError());
      return;
   }

   FileWrite(h, "time", "open", "high", "low", "close", "tickvol", "realvol");
   for(int i = 0; i < n; i++)
      FileWrite(h,
                (long)rates[i].time,
                rates[i].open, rates[i].high, rates[i].low, rates[i].close,
                (long)rates[i].tick_volume,
                (long)rates[i].real_volume);
   FileClose(h);

   g_done = true;
   PrintFormat("ExportBarsCsv: wrote %d bars -> Common\\Files\\%s  (%s .. %s)",
               n, fn, TimeToString(rates[0].time), TimeToString(rates[n - 1].time));
}
