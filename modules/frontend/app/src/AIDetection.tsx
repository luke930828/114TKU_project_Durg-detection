import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { authFetch } from "./auth";
import { ExternalLink } from "./ExternalLink";

const REFRESH_INTERVAL_MS = 30_000;
const CRAWLER_LIMIT = 50;

interface PendingSiteInput {
  url: string;
  score: number;
  riskLevel: string;
  detectedAt: string;
}

interface Props {
  onBack: () => void;
  /**
   * sites 只是「目前這一頁」的資料（一次 50 筆）。
   * pendingTotal 是後端對全部資料算出來的待覆核總數——
   * 兩者不會一樣，顯示筆數時要用 pendingTotal，不要用 sites.length。
   */
  onDetectionsLoaded?: (
    sites: PendingSiteInput[],
    meta: { pendingTotal: number }
  ) => void;
}

interface RepresentativeDetection {
  className: string;
  confidence: number;
  box: [number, number, number, number];
}

// OCR 不在前端顯示。
//
// 圖片裡的文字改成直接送給 NLP 當額外的文字證據（backend/app/utils.py 的
// analyze_ocr_text_with_nlp），影響的是風險分數本身，不是多一個給人看的區塊。
// 承辦人員要看的是「這一頁幾分、為什麼」，不是一堆 'netwt'、'403' 這種
// 從包裝上讀到的碎片——那些對判讀沒有幫助，只會把畫面塞滿。
// 原始的 OCR 結果仍然存在 ai_analysis_results.ocr_results，要追查時查得到。

interface ResultType {
  id: string | number;
  time: string;
  websiteUrl: string;
  content: string;
  drugType: string;
  language: string;
  riskLevel: "critical" | "high" | "medium" | "low";
  score: number;
  caseNumber: string;
  nlpKeywords: string[];
  hasRepresentativeImage: boolean;
  representativeImageBase64: string | null;
  representativeImageDetections: RepresentativeDetection[];
}

interface CrawlerStats {
  total: number;
  high: number;
  medium: number;
  low: number;
}

const EMPTY_STATS: CrawlerStats = {
  total: 0,
  high: 0,
  medium: 0,
  low: 0,
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object";

const getString = (value: unknown, fallback = "") =>
  typeof value === "string" ? value : fallback;

const normalizeKeywords = (value: unknown): string[] => {
  const keywords = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : typeof value === "string"
      ? value.split(/[,，、\n]+/)
      : [];

  return [...new Set(keywords.map((keyword) => keyword.trim()).filter(Boolean))];
};

// 分級由後端決定（utils.py 是唯一定義門檻的地方），前端只負責顯示。
// 這裡以前是 score > 74 / >= 35 自己重算一套——那是第三套門檻，
// 後端怎麼改都沒有作用，2026-08-30 把加權平均改成門檻判定時就是這樣被吃掉的。
const normalizeRiskLevel = (level: string): ResultType["riskLevel"] => {
  const l = (level ?? "").trim();
  if (l.startsWith("極高風險")) return "critical";
  if (l.startsWith("高風險")) return "high";
  if (l.startsWith("中風險")) return "medium";
  return "low";
};

const normalizeRepresentativeDetections = (
  value: unknown
): RepresentativeDetection[] => {
  if (!Array.isArray(value)) return [];

  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    if (!Array.isArray(item.box) || item.box.length !== 4) return [];

    const box = item.box.map(Number);
    if (box.some((coordinate) => !Number.isFinite(coordinate))) return [];

    return [{
      className: getString(item.class_name ?? item.class, "未知類別"),
      confidence: Number(item.confidence ?? 0),
      box: box as [number, number, number, number],
    }];
  });
};

const normalizeResult = (value: unknown, index: number): ResultType | null => {
  if (!isRecord(value)) return null;

  const score = Number(value.score ?? value.risk_score ?? 0);
  const websiteUrl = getString(
    value.websiteUrl ?? value.website_url ?? value.target_url ?? value.url ??
      value.domain_name
  );

  return {
    id: typeof value.id === "string" || typeof value.id === "number"
      ? value.id
      : `detection-${index}`,
    time: getString(
      value.time ?? value.detected_at ?? value.created_at ?? value.discovered_date,
      "時間未提供"
    ),
    websiteUrl,
    content: getString(
      value.content ?? value.description ?? value.summary ??
        value.nlp_details ?? value.yolo_details,
      "AI 發現可疑網站"
    ),
    drugType: getString(value.drugType ?? value.drug_type, "待確認"),
    language: getString(value.language, "未知"),
    riskLevel: normalizeRiskLevel(getString(value.risk_level, "")),
    score: Number.isFinite(score) ? score : 0,
    caseNumber: getString(
      value.caseNumber ?? value.case_number,
      String(value.id ?? "未建立")
    ),
    nlpKeywords: normalizeKeywords(
      value.nlp_details
    ),
    hasRepresentativeImage:
      value.has_representative_image === true ||
      (typeof value.representative_image_base64 === "string" &&
        value.representative_image_base64.trim() !== ""),
    representativeImageBase64:
      typeof value.representative_image_base64 === "string" &&
      value.representative_image_base64.trim()
        ? value.representative_image_base64.replace(/\s/g, "")
        : null,
    representativeImageDetections: normalizeRepresentativeDetections(
      value.representative_image_detections
    ),
  };
};

export function AIDetection({ onBack, onDetectionsLoaded }: Props) {
  const [data, setData] = useState<ResultType[]>([]);
  const [selected, setSelected] = useState<ResultType | null>(null);

  // 代表圖不再夾帶在清單裡——每張 base64 可以到 600 KB，一頁 50 筆就近 10 MB。
  // 改成點開明細時才去拿那一筆的圖。
  const openDetail = useCallback(async (item: ResultType) => {
    setSelected(item);
    if (!item.hasRepresentativeImage || item.representativeImageBase64) return;
    try {
      const response = await authFetch(`/api/crawler/result/${item.id}/image/`);
      if (!response.ok) return;
      const payload = (await response.json()) as {
        representative_image_base64?: unknown;
        representative_image_detections?: unknown;
      };
      const base64 =
        typeof payload.representative_image_base64 === "string"
          ? payload.representative_image_base64.replace(/\s/g, "")
          : "";
      setSelected((current) =>
        current && current.id === item.id
          ? {
              ...current,
              representativeImageBase64: base64 || null,
              representativeImageDetections:
                normalizeRepresentativeDetections(payload.representative_image_detections),
            }
          : current
      );
    } catch (requestError) {
      console.error("[DETAIL_IMAGE_FETCH_FAILED]", {
        id: item.id,
        message: requestError instanceof Error ? requestError.message : "未知錯誤",
      });
    }
  }, []);
  const [filterRisk, setFilterRisk] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);
  const [stats, setStats] = useState<CrawlerStats>(EMPTY_STATS);
  const callbackRef = useRef(onDetectionsLoaded);

  useEffect(() => {
    callbackRef.current = onDetectionsLoaded;
  }, [onDetectionsLoaded]);

  const loadDetections = useCallback(async (page: number) => {
    try {
      const query = new URLSearchParams({
        page: String(page),
        limit: String(CRAWLER_LIMIT),
      });
      const response = await authFetch(`/api/crawler/automated_24h_list/?${query}`, {
        headers: { Accept: "application/json" },
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const payload: unknown = await response.json();
      const rawData = Array.isArray(payload)
        ? payload
        : isRecord(payload) && Array.isArray(payload.data)
          ? payload.data
          : [];
      const pagination = isRecord(payload) && isRecord(payload.pagination)
        ? payload.pagination
        : null;
      const responseStats = isRecord(payload) && isRecord(payload.stats)
        ? payload.stats
        : null;
      const responsePage = Number(pagination?.current_page ?? page);
      const responseTotalPages = Number(
        pagination?.total_pages ?? (rawData.length > 0 ? 1 : 0)
      );
      const results = rawData
        .map(normalizeResult)
        .filter((item): item is ResultType => item !== null)
        .sort((first, second) => second.score - first.score);

      setData(results);
      setStats({
        total: Number(responseStats?.total ?? 0),
        high: Number(responseStats?.high ?? 0),
        medium: Number(responseStats?.medium ?? 0),
        low: Number(responseStats?.low ?? 0),
      });
      setCurrentPage(Number.isFinite(responsePage) && responsePage > 0 ? responsePage : page);
      setTotalPages(
        Number.isFinite(responseTotalPages) && responseTotalPages >= 0
          ? responseTotalPages
          : 0
      );
      setError(null);
      setLastUpdated(new Date());

      callbackRef.current?.(
        results
          .filter((item) => item.websiteUrl)
          .map((item) => ({
            url: item.websiteUrl,
            score: item.score,
            riskLevel: getRiskText(item.riskLevel),
            detectedAt: item.time,
          })),
        { pendingTotal: Number(responseStats?.medium ?? 0) }
      );
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "未知錯誤";
      setError(`無法取得 24小時AI自動識別資料：${message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    loadDetections(currentPage);
    const timer = window.setInterval(
      () => loadDetections(currentPage),
      REFRESH_INTERVAL_MS
    );
    return () => window.clearInterval(timer);
  }, [currentPage, loadDetections]);

  const handleRefresh = async () => {
    setLoading(true);
    await loadDetections(currentPage);
  };

  const changePage = (page: number) => {
    if (loading || page < 1 || page > totalPages || page === currentPage) return;
    setSelected(null);
    setCurrentPage(page);
  };

  const filtered =
    filterRisk === "all"
      ? data
      : data.filter((item) => item.riskLevel === filterRisk);

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#2B4C7E] to-[#1a2f4f] p-6">
      <div className="max-w-6xl mx-auto">
        <div className="text-center mb-10">
          <h1 className="text-white text-4xl font-bold mb-2">
            多模態毒品交易防制系統
          </h1>
          <p className="text-white/80 text-lg">24小時AI自動識別－爬蟲判讀結果</p>
        </div>

        <div className="bg-white rounded-3xl p-8 shadow-2xl">
          <div className="flex flex-wrap justify-between gap-3 mb-6">
            <button
              type="button"
              onClick={onBack}
              className="flex items-center gap-2 text-[#2B4C7E] hover:text-blue-400"
            >
              <ArrowLeft />返回主頁
            </button>
            <div className="flex items-center gap-3">
              <span className="text-sm text-gray-400">
                {lastUpdated
                  ? `最後更新：${lastUpdated.toLocaleTimeString("zh-TW")}`
                  : "正在取得最新資料"}
              </span>
              <button
                type="button"
                onClick={handleRefresh}
                disabled={loading}
                className="flex items-center gap-1.5 rounded-lg bg-[#2B4C7E] px-3 py-2 text-sm font-medium text-white transition hover:bg-[#1a2f4f] disabled:cursor-not-allowed disabled:opacity-60"
              >
                <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
                刷新
              </button>
            </div>
          </div>

          {error && (
            <div className="mb-6 rounded-xl border border-red-200 bg-red-50 p-4 text-red-700">
              {error}。請確認 Tailscale 與後端服務是否已啟動。
            </div>
          )}

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
            <Stat title="總筆數" value={stats.total} />
            <Stat title="極高風險" value={stats.high} color="text-red-600" />
            <Stat title="待覆核" value={stats.medium} color="text-yellow-600" />
            <Stat title="低風險" value={stats.low} color="text-green-600" />
          </div>

          <div className="mb-6">
            <select
              value={filterRisk}
              onChange={(event) => setFilterRisk(event.target.value)}
              className="border-2 border-gray-200 rounded-lg p-2 focus:border-[#2B4C7E]"
            >
              <option value="all">全部</option>
              <option value="critical">極高風險</option>
              <option value="high">高風險 (優先覆核)</option>
              <option value="medium">中風險 (建議覆核)</option>
              <option value="low">低風險</option>
            </select>
          </div>

          {loading ? (
            <div className="py-16 text-center text-gray-500">正在取得爬蟲識別資料…</div>
          ) : filtered.length === 0 ? (
            <div className="py-16 text-center border-2 border-dashed rounded-xl text-gray-400">
              目前沒有符合條件的可疑網站
            </div>
          ) : (
            <div className="space-y-4">
              {filtered.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  onClick={() => openDetail(item)}
                  className="w-full text-left border-2 border-gray-200 p-5 rounded-2xl hover:shadow-lg hover:border-[#2B4C7E] transition"
                >
                  <div className="min-w-0">
                    <p className="text-sm text-gray-400">{item.time}</p>
                    {item.websiteUrl && (
                      <p className="font-bold text-blue-600 break-all mt-1"><ExternalLink url={item.websiteUrl} /></p>
                    )}
                  </div>
                  <div className="mt-4 flex items-center gap-3">
                    <div className="min-w-0 flex-1 bg-gray-200 h-2 rounded-full overflow-hidden">
                      <div
                        className={`${getRiskProgressColor(item.riskLevel)} h-2 rounded-full`}
                        style={{ width: `${Math.min(100, Math.max(0, item.score))}%` }}
                      />
                    </div>
                    <span className={`w-14 shrink-0 text-right text-lg font-bold ${getRiskScoreColor(item.riskLevel)}`}>
                      {item.score}%
                    </span>
                  </div>
                </button>
              ))}
            </div>
          )}

          {totalPages > 0 && (
            <div className="mt-8 flex flex-wrap items-center justify-center gap-4 border-t border-gray-100 pt-6">
              <button
                type="button"
                onClick={() => changePage(currentPage - 1)}
                disabled={loading || currentPage <= 1}
                className="rounded-lg bg-[#2B4C7E] px-4 py-2 font-medium text-white transition hover:bg-[#1a2f4f] disabled:cursor-not-allowed disabled:bg-gray-300"
              >
                上一頁
              </button>
              <span className="min-w-44 text-center font-medium text-gray-600">
                第 {currentPage} 頁 / 共 {totalPages} 頁
              </span>
              <button
                type="button"
                onClick={() => changePage(currentPage + 1)}
                disabled={loading || currentPage >= totalPages}
                className="rounded-lg bg-[#2B4C7E] px-4 py-2 font-medium text-white transition hover:bg-[#1a2f4f] disabled:cursor-not-allowed disabled:bg-gray-300"
              >
                下一頁
              </button>
            </div>
          )}
        </div>

        {selected && (
          <div className="fixed inset-0 z-50 bg-black/60 flex justify-center items-center p-4">
            <div className="bg-white w-full max-w-3xl max-h-[90vh] rounded-2xl overflow-y-auto">
              <div className="bg-[#2B4C7E] text-white p-5 flex justify-between">
                <div><h2 className="text-xl font-bold">詳細分析</h2><p>案件編號：{selected.caseNumber}</p></div>
                <button type="button" onClick={() => setSelected(null)}>✕</button>
              </div>
              <div className="p-6">
                {selected.websiteUrl && (
                  <p className="mb-3 break-all">
                    <strong>網站：</strong>
                    <ExternalLink url={selected.websiteUrl} className="text-blue-600" />
                  </p>
                )}
                <p className="text-lg mb-4">風險分數：<span className="font-bold">{selected.score}%</span></p>
                <h3 className="font-semibold mb-2">NLP 關鍵字</h3>
                <div className="mb-4 flex flex-wrap gap-2">
                  {selected.nlpKeywords.length > 0 ? (
                    selected.nlpKeywords.map((keyword) => (
                      <span
                        key={keyword}
                        className="rounded-full bg-blue-100 px-3 py-1 text-sm font-medium text-blue-700"
                      >
                        {keyword}
                      </span>
                    ))
                  ) : (
                    <span className="text-gray-400">未提供</span>
                  )}
                </div>
                <h3 className="font-semibold mb-2">AI 分析</h3>
                {selected.representativeImageBase64 ? (
                  <div className="relative inline-block max-w-full overflow-hidden rounded-xl border border-blue-200 bg-gray-100">
                    <img
                      src={`data:image/jpeg;base64,${selected.representativeImageBase64}`}
                      alt="AI 辨識代表圖"
                      className="block h-auto max-h-[55vh] max-w-full"
                    />
                    {selected.representativeImageDetections.map((detection, index) => {
                      const [x1, y1, x2, y2] = detection.box.map((coordinate) =>
                        Math.min(1, Math.max(0, coordinate))
                      );
                      if (x2 <= x1 || y2 <= y1) return null;

                      const confidence = detection.confidence <= 1
                        ? detection.confidence * 100
                        : detection.confidence;

                      return (
                        <div
                          key={`${detection.className}-${index}`}
                          className="absolute border-2 border-cyan-400"
                          style={{
                            left: `${x1 * 100}%`,
                            top: `${y1 * 100}%`,
                            width: `${(x2 - x1) * 100}%`,
                            height: `${(y2 - y1) * 100}%`,
                          }}
                        >
                          <span className="absolute left-0 top-0 whitespace-nowrap rounded-br bg-cyan-400 px-1.5 py-0.5 text-xs font-bold text-cyan-950">
                            {detection.className} {Math.round(confidence)}%
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="aspect-video w-full rounded-xl border-2 border-dashed border-gray-300 bg-gray-50 flex items-center justify-center px-6 text-center text-gray-400">
                    沒有圖可顯示
                  </div>
                )}
                <button type="button" onClick={() => setSelected(null)} className="mt-6 bg-gray-200 px-4 py-2 rounded-lg">關閉</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function getRiskText(level: ResultType["riskLevel"]) {
  if (level === "critical") return "極高風險";
  if (level === "high") return "高風險 (優先人工覆核)";
  if (level === "medium") return "中風險 (建議人工覆核)";
  return "低風險";
}

function getRiskProgressColor(level: ResultType["riskLevel"]) {
  if (level === "critical") return "bg-red-600";
  if (level === "high") return "bg-orange-500";
  if (level === "medium") return "bg-amber-500";
  return "bg-green-500";
}

function getRiskScoreColor(level: ResultType["riskLevel"]) {
  if (level === "critical") return "text-red-700";
  if (level === "high") return "text-orange-600";
  if (level === "medium") return "text-amber-600";
  return "text-green-600";
}

interface StatProps {
  title: string;
  value: number;
  color?: string;
}

function Stat({ title, value, color = "" }: StatProps) {
  return (
    <div className="bg-gray-50 p-4 rounded-xl text-center shadow-sm">
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
      <div className="text-sm text-gray-500">{title}</div>
    </div>
  );
}
