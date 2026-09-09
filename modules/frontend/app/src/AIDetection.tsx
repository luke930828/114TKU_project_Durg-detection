import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, RefreshCw } from "lucide-react";
import { authFetch } from "./auth";
import { ExternalLink } from "./ExternalLink";

const REFRESH_INTERVAL_MS = 30_000;
const CRAWLER_LIMIT = 50;
// 展開一個網域時一次抓多少頁。目前單一網域最多 50 筆，200 是留給之後成長的餘裕。
const DOMAIN_PAGE_LIMIT = 200;

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

// OCR 不在前端顯示：圖片裡的文字直接送給 NLP 當額外的文字證據，影響的是風險
// 分數本身，不是多一個給人看的區塊。承辦人員要看的是「這一頁幾分、為什麼」，
// 不是一堆從包裝上讀到的碎片。原始結果仍存在 ai_analysis_results.ocr_results。

interface ResultType {
  id: string | number;
  yoloScore: number;
  // 「人確認過」跟「模型判幾級」是兩件事，要分開顯示
  humanVerified: boolean;
  verifiedAt: string | null;
  time: string;
  websiteUrl: string;
  content: string;
  drugType: string;
  language: string;
  // verified 不是「更嚴重的一級」，是另一個維度。放同一個聯集是因為
  // 清單上這格只放得下一個標籤，而「有人確定是毒品站」比等級更該被看到。
  riskLevel: "verified" | "critical" | "high" | "medium" | "low";
  score: number;
  caseNumber: string;
  nlpKeywords: string[];
  hasRepresentativeImage: boolean;
  representativeImageBase64: string | null;
  representativeImageDetections: RepresentativeDetection[];
}

// 清單以「網域」為一列，同網域的各個網頁收在底下，點開才去查。
// 一個網域動輒幾十頁，平鋪的話一頁 50 筆常常全部是同一個站。
interface DomainRow {
  domain: string;
  pageCount: number;
  score: number;
  yoloScore: number;
  riskLevel: ResultType["riskLevel"];
  // 這個網域底下有幾頁被人確認過
  verifiedCount: number;
  // 模型自己怎麼看。已確認的網域主標籤會蓋掉等級，
  // 這個欄位讓模型的判定仍然看得到。
  modelRiskLevel: ResultType["riskLevel"];
  date: string;
}

interface CrawlerStats {
  total: number;
  high: number;
  medium: number;
  low: number;
}

// 兩個分數分開顯示，不合成一個。等級是二維判斷（文字過門檻、影像有沒有附和），
// 只顯示一個分數的話會出現「文字 100 分卻是高風險」排在「文字 95 分的極高風險」
// 後面，看起來自相矛盾（實際有 607 筆）。合成的做法也實測過，排序都比純文字差。
function ScorePair({ text, image }: { text: number; image: number }) {
  return (
    <span className="font-mono text-sm text-gray-500 whitespace-nowrap">
      文字 <span className="font-bold text-gray-700">{text}</span>
      <span className="mx-1.5 text-gray-300">·</span>
      影像 <span className="font-bold text-gray-700">{image}</span>
    </span>
  );
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
  // 後端網域列會回「已人工確認」。少了這行會掉進最後的 return "low"，
  // 已確定是毒品站的網域顯示成綠色低風險——那是最糟的一種錯。
  if (l.startsWith("已人工確認") || l.startsWith("覆核")) return "verified";
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
  const yoloScore = Number(value.yolo_score ?? 0);
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
    yoloScore: Number.isFinite(yoloScore) ? yoloScore : 0,
    humanVerified: value.human_verified === true,
    verifiedAt: typeof value.human_verified_at === "string"
      ? value.human_verified_at
      : null,
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

const normalizeDomainRow = (value: unknown): DomainRow | null => {
  if (!isRecord(value)) return null;
  const domain = getString(value.domain);
  if (!domain) return null;
  const score = Number(value.risk_score ?? 0);
  const yoloScore = Number(value.yolo_score ?? 0);
  return {
    domain,
    pageCount: Number(value.page_count ?? 0),
    score: Number.isFinite(score) ? score : 0,
    yoloScore: Number.isFinite(yoloScore) ? yoloScore : 0,
    riskLevel: normalizeRiskLevel(getString(value.risk_level, "")),
    verifiedCount: Number(value.verified_count ?? 0),
    modelRiskLevel: normalizeRiskLevel(getString(value.model_risk_level, "")),
    date: getString(value.discovered_date, "時間未提供"),
  };
};

export function AIDetection({ onBack, onDetectionsLoaded }: Props) {
  const [data, setData] = useState<DomainRow[]>([]);
  const [selected, setSelected] = useState<ResultType | null>(null);
  // 展開中的網域，以及已經抓回來的網頁清單（同一個網域不重複抓）
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pages, setPages] = useState<Record<string, ResultType[]>>({});
  const [pagesLoading, setPagesLoading] = useState<string | null>(null);
  const [pagesError, setPagesError] = useState<string | null>(null);

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
        group: "domain",
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
      // 順序由後端決定（最嚴重的網域在前），前端不要再排一次
      const results = rawData
        .map(normalizeDomainRow)
        .filter((item): item is DomainRow => item !== null);

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
        results.map((item) => ({
          url: item.domain,
          score: item.score,
          riskLevel: getRiskText(item.riskLevel),
          detectedAt: item.date,
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

  // 展開時才抓這個網域底下的網頁。抓過就留著，重複開合不會再打一次。
  const toggleDomain = useCallback(async (domain: string) => {
    if (expanded === domain) {
      setExpanded(null);
      return;
    }
    setExpanded(domain);
    setPagesError(null);
    if (pages[domain]) return;

    setPagesLoading(domain);
    try {
      const query = new URLSearchParams({
        domain,
        page: "1",
        limit: String(DOMAIN_PAGE_LIMIT),
      });
      const response = await authFetch(
        `/api/crawler/automated_24h_list/?${query}`,
        { headers: { Accept: "application/json" } }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload: unknown = await response.json();
      const rawData =
        isRecord(payload) && Array.isArray(payload.data) ? payload.data : [];
      const rows = rawData
        .map(normalizeResult)
        .filter((item): item is ResultType => item !== null);
      setPages((current) => ({ ...current, [domain]: rows }));
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "未知錯誤";
      setPagesError(`無法取得 ${domain} 的網頁清單：${message}`);
    } finally {
      setPagesLoading(null);
    }
  }, [expanded, pages]);

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
    setExpanded(null);
    setCurrentPage(page);
  };

  // 「覆核過」不是等級，單獨判斷：底下有任何一頁被確認過就算。
  // 用 riskLevel === "verified" 也行，但那依賴後端把 worst 算成 -1，藏太深。
  const filtered =
    filterRisk === "all"
      ? data
      : filterRisk === "verified"
        ? data.filter((item) => item.verifiedCount > 0)
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
              <option value="verified">覆核確定為毒品網站</option>
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
              {filtered.map((item) => {
                const isOpen = expanded === item.domain;
                const childPages = pages[item.domain] ?? [];
                return (
                  <div
                    key={item.domain}
                    className={`border-2 rounded-2xl transition ${
                      isOpen
                        ? "border-[#2B4C7E] shadow-lg"
                        : "border-gray-200 hover:border-[#2B4C7E] hover:shadow-lg"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleDomain(item.domain)}
                      aria-expanded={isOpen}
                      className="w-full text-left p-5"
                    >
                      <div className="flex items-start gap-3">
                        <ChevronRight
                          className={`mt-1 shrink-0 text-gray-400 transition-transform ${
                            isOpen ? "rotate-90" : ""
                          }`}
                          size={20}
                        />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-gray-400">
                            最後發現：{item.date}
                          </p>
                          <p className="font-bold text-[#2B4C7E] break-all mt-1">
                            {item.domain}
                          </p>
                          <p className="text-sm text-gray-500 mt-1">
                            這個網域底下有 {item.pageCount} 個網頁被判讀
                            {/* 摘要列顯示的是「最嚴重的那一頁」，不是平均——
                                分流時要先看最該看的，平均會把一頁高分稀釋掉 */}
                            ，以下分數是其中最高的一頁
                          </p>
                        </div>
                      </div>
                      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2">
                        <span className={`shrink-0 font-bold ${getRiskScoreColor(item.riskLevel)}`}>
                          {getRiskText(item.riskLevel)}
                        </span>
                        {item.verifiedCount > 0 && (
                          <span className="shrink-0 text-xs text-gray-400">
                            {item.verifiedCount} 頁已確認　模型判定：
                            {getRiskText(item.modelRiskLevel)}
                          </span>
                        )}
                        <div className="min-w-0 flex-1 bg-gray-200 h-2 rounded-full overflow-hidden">
                          <div
                            className={`${getRiskProgressColor(item.riskLevel)} h-2 rounded-full`}
                            style={{ width: `${Math.min(100, Math.max(0, item.score))}%` }}
                          />
                        </div>
                        <ScorePair text={item.score} image={item.yoloScore} />
                      </div>
                    </button>

                    {isOpen && (
                      <div className="border-t border-gray-200 bg-gray-50 px-5 py-4 rounded-b-2xl">
                        {pagesLoading === item.domain ? (
                          <p className="text-gray-400 text-sm">載入這個網域的網頁中...</p>
                        ) : pagesError ? (
                          <p className="text-red-500 text-sm">{pagesError}</p>
                        ) : childPages.length === 0 ? (
                          <p className="text-gray-400 text-sm">沒有可顯示的網頁</p>
                        ) : (
                          <div className="space-y-2">
                            {childPages.map((child) => (
                              <button
                                type="button"
                                key={child.id}
                                onClick={() => openDetail(child)}
                                className="w-full text-left rounded-xl border border-gray-200 bg-white px-4 py-3 hover:border-[#2B4C7E] transition"
                              >
                                <div className="flex items-center gap-3">
                                  <div className="min-w-0 flex-1">
                                    <p className="text-sm text-blue-600 break-all">
                                      <ExternalLink url={child.websiteUrl} />
                                    </p>
                                    <p className="text-xs text-gray-400 mt-1">
                                      {child.time}　案件編號：{child.caseNumber}
                                    </p>
                                  </div>
                                  <span className="shrink-0 flex items-center gap-3">
                                    {child.humanVerified ? (
                                      <span className="flex flex-col items-end leading-tight">
                                        <span
                                          title={child.verifiedAt
                                            ? `已於 ${new Date(child.verifiedAt).toLocaleString("zh-TW", { hour12: false })} 由承辦人員確認`
                                            : "已由承辦人員確認"}
                                          className={`text-sm font-bold ${getRiskScoreColor("verified")}`}
                                        >
                                          {getRiskText("verified")}
                                        </span>
                                        <span className="text-xs text-gray-400">
                                          模型判定：{getRiskText(child.riskLevel)}
                                        </span>
                                      </span>
                                    ) : (
                                      <span className={`text-sm font-bold ${getRiskScoreColor(child.riskLevel)}`}>
                                        {getRiskText(child.riskLevel)}
                                      </span>
                                    )}
                                    <ScorePair text={child.score} image={child.yoloScore} />
                                  </span>
                                </div>
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
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
                <p className="text-lg mb-4">
                  文字分數：<span className="font-bold">{selected.score}</span>
                  <span className="mx-2 text-gray-300">·</span>
                  影像分數：<span className="font-bold">{selected.yoloScore}</span>
                </p>
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
  if (level === "verified") return "覆核確定為毒品網站";
  if (level === "critical") return "極高風險";
  if (level === "high") return "高風險 (優先人工覆核)";
  if (level === "medium") return "中風險 (建議人工覆核)";
  return "低風險";
}

function getRiskProgressColor(level: ResultType["riskLevel"]) {
  if (level === "verified") return "bg-violet-600";
  if (level === "critical") return "bg-red-600";
  if (level === "high") return "bg-orange-500";
  if (level === "medium") return "bg-amber-500";
  return "bg-green-500";
}

function getRiskScoreColor(level: ResultType["riskLevel"]) {
  // 紫色刻意不在紅→橙→琥珀→綠那條色階上：它不是「更嚴重」，
  // 而是換了一種依據（人的結論，不是模型的分數）。
  if (level === "verified") return "text-violet-700";
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
