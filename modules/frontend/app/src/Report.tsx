import { useState } from "react";
import {
  ArrowLeft,
  FileSpreadsheet,
  Calendar,
} from "lucide-react";
import { authFetch, getErrorMessage } from "./auth";

interface Props {
  onBack: () => void;
}

type ReportRange = "season" | "year" | "custom";
type Quarter = "Q1" | "Q2" | "Q3" | "Q4";

export function Report({ onBack }: Props) {
  const [range, setRange] = useState<ReportRange | null>(null);

  const [quarter, setQuarter] = useState<Quarter | null>(null);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const currentYear = new Date().getFullYear();

  // 判斷資料是否選擇完成
  const isReady =
    Boolean(range) &&
    (range !== "season" || Boolean(quarter)) &&
    (range !== "custom" || Boolean(startDate && endDate));

  const handleRangeChange = (selectedRange: ReportRange) => {
    setRange(selectedRange);

    if (selectedRange !== "season") {
      setQuarter(null);
    }

    if (selectedRange !== "custom") {
      setStartDate("");
      setEndDate("");
    }
  };

  // 依據季度取得開始與結束日期
  const getQuarterDates = (selectedQuarter: Quarter) => {
    switch (selectedQuarter) {
      case "Q1":
        return {
          startDate: `${currentYear}-01-01`,
          endDate: `${currentYear}-03-31`,
        };

      case "Q2":
        return {
          startDate: `${currentYear}-04-01`,
          endDate: `${currentYear}-06-30`,
        };

      case "Q3":
        return {
          startDate: `${currentYear}-07-01`,
          endDate: `${currentYear}-09-30`,
        };

      case "Q4":
        return {
          startDate: `${currentYear}-10-01`,
          endDate: `${currentYear}-12-31`,
        };
    }
  };

  // 使用驗證 Token 取得檔案後觸發瀏覽器下載
  const downloadExcelReport = async (
    reportStartDate: string,
    reportEndDate: string
  ) => {
    const queryString = new URLSearchParams({
      start_date: reportStartDate,
      end_date: reportEndDate,
    });

    try {
      const response = await authFetch(
        `/api/export/ai_results_excel/?${queryString.toString()}`,
        { headers: { Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" } }
      );

      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }

      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") ?? "";
      const filenameMatch = disposition.match(/filename\*?=(?:UTF-8''|\")?([^";]+)/i);
      const filename = filenameMatch
        ? decodeURIComponent(filenameMatch[1].replace(/"$/, ""))
        : `ai_results_${reportStartDate}_${reportEndDate}.xlsx`;
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (downloadError) {
      const message = downloadError instanceof Error
        ? downloadError.message
        : "未知錯誤";
      alert(
        message === "AUTH_TOKEN_MISSING"
          ? "登入狀態已失效，請重新登入。"
          : `報表下載失敗：${message}`
      );
    }
  };

  const handleGenerate = async () => {
    if (!range) return;

    // 季度報表
    if (range === "season") {
      if (!quarter) {
        alert("請選擇 Q1、Q2、Q3 或 Q4");
        return;
      }

      const dates = getQuarterDates(quarter);

      await downloadExcelReport(dates.startDate, dates.endDate);
      return;
    }

    // 年度報表
    if (range === "year") {
      await downloadExcelReport(
        `${currentYear}-01-01`,
        `${currentYear}-12-31`
      );
      return;
    }

    // 自訂範圍
    if (range === "custom") {
      if (!startDate || !endDate) {
        alert("請先選擇完整的開始日期與結束日期！");
        return;
      }

      if (startDate > endDate) {
        alert("開始日期不能晚於結束日期！");
        return;
      }

      await downloadExcelReport(startDate, endDate);
      return;
    }

  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#2B4C7E] to-[#1a2f4f] flex justify-center items-center p-6">
      <div className="bg-white rounded-2xl shadow-xl p-8 w-full max-w-2xl">
        {/* 返回 */}
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-2 text-gray-500 hover:text-blue-500 mb-6 transition"
        >
          <ArrowLeft className="w-5 h-5" />
          返回主頁
        </button>

        <div className="mb-4 flex items-center gap-3">
          <FileSpreadsheet className="h-8 w-8 text-green-500" />
          <div>
            <h1 className="text-xl font-bold text-gray-800">Excel 報表匯出</h1>
            <p className="text-sm text-gray-500">選擇資料時間範圍後下載 Excel 檔案</p>
          </div>
        </div>

        {/* 說明 */}
        {/*
          原本這裡寫「包含AI偵測案件統計、毒品類別分布、趨勢分析圖表、
          關鍵績效指標(KPI)及執法建議」——那四項後端都沒有做。
          export.py 產出的是單一工作表「AI分析總表」，四個欄位而已。
          說明文字跟實際下載的檔案對不起來，使用者會以為檔案壞了。
        */}
        <div className="bg-blue-50 border border-blue-200 p-4 rounded-lg mb-6 text-sm text-blue-700">
          <p className="font-semibold mb-1">匯出內容：AI 分析總表（單一工作表）</p>
          <p>
            每一列是一筆 AI 分析結果，欄位為
            <span className="font-medium">案件編號、網址、風險分數、風險等級</span>。
            時間範圍以分析建立時間篩選；該區間沒有資料時不會產生檔案。
          </p>
        </div>

        {/* 時間範圍 */}
        <h2 className="text-lg font-bold text-gray-800 mb-4">
          選擇時間範圍
        </h2>

        <div className="grid grid-cols-3 gap-4 mb-4">
          {/* 季度報表 */}
          <button
            type="button"
            onClick={() => handleRangeChange("season")}
            className={`border rounded-xl p-5 text-center transition ${
              range === "season"
                ? "border-blue-500 bg-blue-50"
                : "border-gray-200 hover:border-blue-300"
            }`}
          >
            <div className="font-bold">季度報表</div>

            <div className="text-sm text-gray-400">
              Q1-Q4
            </div>
          </button>

          {/* 年度報表 */}
          <button
            type="button"
            onClick={() => handleRangeChange("year")}
            className={`border rounded-xl p-5 text-center transition ${
              range === "year"
                ? "border-blue-500 bg-blue-50"
                : "border-gray-200 hover:border-blue-300"
            }`}
          >
            <div className="font-bold">年度報表</div>

            <div className="text-sm text-gray-400">
              {currentYear} 全年
            </div>
          </button>

          {/* 自訂範圍 */}
          <button
            type="button"
            onClick={() => handleRangeChange("custom")}
            className={`border rounded-xl p-5 text-center transition ${
              range === "custom"
                ? "border-blue-500 bg-blue-50"
                : "border-gray-200 hover:border-blue-300"
            }`}
          >
            <Calendar className="w-5 h-5 mx-auto mb-1 text-gray-500" />

            <div className="font-bold">自訂範圍</div>

            <div className="text-sm text-gray-400">
              選擇日期
            </div>
          </button>
        </div>

        {/* 選擇季度 */}
        {range === "season" && (
          <div className="mb-8 p-4 bg-blue-50 border border-blue-200 rounded-xl">
            <div className="text-sm font-medium text-gray-700 mb-3">
              請選擇 {currentYear} 年季度
            </div>

            <div className="grid grid-cols-4 gap-3">
              {(["Q1", "Q2", "Q3", "Q4"] as Quarter[]).map(
                (item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setQuarter(item)}
                    className={`py-3 rounded-lg border font-bold transition ${
                      quarter === item
                        ? "border-blue-500 bg-blue-500 text-white"
                        : "border-gray-300 bg-white text-gray-700 hover:border-blue-400"
                    }`}
                  >
                    {item}
                  </button>
                )
              )}
            </div>

            {quarter && (
              <div className="mt-3 text-sm text-blue-700">
                已選擇：{currentYear} 年 {quarter}
              </div>
            )}
          </div>
        )}

        {/* 自訂日期 */}
        {range === "custom" && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 p-4 mb-8 bg-blue-50 border border-blue-200 rounded-xl">
            <div>
              <label
                htmlFor="startDate"
                className="block text-sm font-medium text-gray-700 mb-2"
              >
                開始日期
              </label>

              <input
                id="startDate"
                type="date"
                value={startDate}
                max={endDate || undefined}
                onChange={(event) =>
                  setStartDate(event.target.value)
                }
                className="w-full px-3 py-2.5 bg-white border border-gray-300 rounded-lg text-gray-700 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-200"
              />
            </div>

            <div>
              <label
                htmlFor="endDate"
                className="block text-sm font-medium text-gray-700 mb-2"
              >
                結束日期
              </label>

              <input
                id="endDate"
                type="date"
                value={endDate}
                min={startDate || undefined}
                onChange={(event) =>
                  setEndDate(event.target.value)
                }
                className="w-full px-3 py-2.5 bg-white border border-gray-300 rounded-lg text-gray-700 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-200"
              />
            </div>
          </div>
        )}

        {/* 年度報表提示 */}
        {range === "year" && (
          <div className="p-4 mb-8 bg-blue-50 border border-blue-200 rounded-xl text-sm text-blue-700">
            將產生 {currentYear}-01-01 至 {currentYear}-12-31
            的年度報表。
          </div>
        )}

        {/* 尚未選範圍時保留間距 */}
        {!range && <div className="mb-8" />}

        {/* 生成報表 */}
        <button
          type="button"
          onClick={handleGenerate}
          disabled={!isReady}
          className={`w-full py-3 rounded-xl font-bold transition ${
            isReady
              ? "bg-blue-500 hover:bg-blue-600 text-white"
              : "bg-gray-200 text-gray-400 cursor-not-allowed"
          }`}
        >
          下載 Excel 報表
        </button>
      </div>
    </div>
  );
}
