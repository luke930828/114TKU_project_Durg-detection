import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Check, ShieldAlert, ShieldCheck } from "lucide-react";
import { authFetch, isAdmin, getErrorMessage } from "./auth";
import { hasMaliciousInput, MALICIOUS_INPUT_MESSAGE } from "./inputSecurity";
import { ExternalLink } from "./ExternalLink";

export interface PendingWebsite {
  /** ai_analysis_results.id——覆核時要用它呼叫後端 */
  id?: number;
  url: string;
  score: number;
  riskLevel: string;
  detectedAt: string;
}

interface WhitelistEntry {
  id: string | number;
  url: string;
  title: string;
  reason: string;
  addedBy: string;
  createdAt: string;
  /** 一般新增 / 誤判回報 */
  source: string;
}

const normalizeWhitelistEntry = (
  value: unknown,
  index: number
): WhitelistEntry | null => {
  if (!value || typeof value !== "object") return null;
  const entry = value as Record<string, unknown>;
  const id = entry.id ?? entry.whitelist_id ?? `whitelist-${index}`;
  if (typeof entry.url !== "string" || !entry.url.trim()) return null;

  return {
    id: typeof id === "string" || typeof id === "number"
      ? id
      : `whitelist-${index}`,
    url: entry.url,
    title: typeof entry.title === "string" ? entry.title : "未提供",
    reason: typeof entry.reason === "string" && entry.reason.trim()
      ? entry.reason
      : "無",
    addedBy: typeof entry.added_by === "string" ? entry.added_by : "未知",
    createdAt: typeof entry.created_at === "string"
      ? entry.created_at.slice(0, 10)
      : "",
    source: typeof entry.source === "string" && entry.source.trim()
      ? entry.source
      : "一般新增",
  };
};

const normalizeSites = (value: unknown): PendingWebsite[] => {
  if (!Array.isArray(value)) return [];
  return value.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const r = row as Record<string, unknown>;
    const url = typeof r.domain_name === "string" ? r.domain_name : "";
    if (!url) return [];
    const score = Number(r.risk_score);
    const id = Number(r.id);
    return [{
      id: Number.isFinite(id) ? id : undefined,
      url,
      score: Number.isFinite(score) ? score : 0,
      riskLevel: typeof r.risk_level === "string" ? r.risk_level : "",
      detectedAt: typeof r.discovered_date === "string" ? r.discovered_date : "",
    }];
  });
};

interface Props {
  onBack: () => void;
  // blacklist / pendingSites / pendingTotal 已經移除：那三個是 App.tsx 的
  // 記憶體狀態，只有開過「AI 偵測」頁面才會被填入、且只填當時那一頁，
  // 重新整理就歸零。現在這兩個清單由本元件直接查後端。
  /** 覆核完一筆時通知父層更新總數。實際寫入由本元件直接打後端。 */
  onReviewed?: () => void;
}

export default function WebsiteQuery({
  onBack,
  onReviewed,
}: Props) {
  // 名單的「新增」開放給一般人員，「刪除」只有管理員。
  // 這只決定畫面上要不要出現按鈕；真正的權限由後端的 verify_admin 把關。
  const userIsAdmin = isAdmin();
  const [tab, setTab] = useState<"black" | "white" | "pending">("pending");
  const [input, setInput] = useState("");
  const [whiteUrl, setWhiteUrl] = useState("");
  const [whiteTitle, setWhiteTitle] = useState("");
  const [whiteReason, setWhiteReason] = useState("");
  // 這次表單是從哪裡填的。
  //
  // 「AI 判過高風險、被人推翻」＝誤判回報，那是重訓模型要用的困難負樣本；
  // 「人主動排除、AI 沒判過」＝一般新增。分界是資料的本質，不是操作路徑——
  // 先前是用路徑分的（黑名單分頁按的算誤判、待確認分頁按的算一般新增），
  // 但待確認裡的網站同樣是 AI 判高風險才會在那裡，記成「一般新增」等於
  // 把誤判樣本混進「正常排除」，重訓時就分不出模型到底錯在哪。
  const [whiteSource, setWhiteSource] = useState("一般新增");
  // 黑名單與待確認直接查後端。
  //
  // 以前這兩個清單是 App.tsx 的 useState，初始值還是寫死的假資料
  // （dark-market-x.onion / google.com），而且只有開啟「AI 偵測」頁面時
  // 才會被填入、只填當時載入的那一頁 50 筆，重新整理就歸零。
  // 所以「待確認 11 筆」從來不是待辦總量，是那一頁裡剛好有幾筆。
  // 人工黑名單（blacklist_websites），跟 AI 推導出來的那份分開顯示。
  const [manualBlacklist, setManualBlacklist] = useState<WhitelistEntry[]>([]);
  const [blackTitle, setBlackTitle] = useState("");
  const [blackReason, setBlackReason] = useState("");
  const [blackSaving, setBlackSaving] = useState(false);
  const [blackSearch, setBlackSearch] = useState("");
  // 三個清單的搜尋各自獨立。共用一個關鍵字的話，在待確認裡找一個網址會
  // 順手把黑名單和白名單也篩掉，看起來像資料不見了。
  const [pendingSearch, setPendingSearch] = useState("");
  const [whiteSearch, setWhiteSearch] = useState("");
  // 批次覆核的勾選。用 id 而不是 url——後端是用 ai_analysis_results.id 定位的，
  // 而且同一個網域可能有多筆不同頁面。
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [batchSaving, setBatchSaving] = useState(false);
  const [remoteBlacklist, setRemoteBlacklist] = useState<PendingWebsite[]>([]);
  const [remoteBlacklistTotal, setRemoteBlacklistTotal] = useState(0);
  const [remotePending, setRemotePending] = useState<PendingWebsite[]>([]);
  const [remotePendingTotal, setRemotePendingTotal] = useState(0);
  const [bucketLoading, setBucketLoading] = useState(true);
  const [whitelistEntries, setWhitelistEntries] = useState<WhitelistEntry[]>([]);
  const [whitelistLoading, setWhitelistLoading] = useState(true);
  const [whitelistError, setWhitelistError] = useState<string | null>(null);
  const [whitelistSaving, setWhitelistSaving] = useState(false);

  const loadManualBlacklist = useCallback(async (keyword = "") => {
    try {
      const qs = keyword.trim() ? `?q=${encodeURIComponent(keyword.trim())}` : "";
      const response = await authFetch(`/api/blacklist/${qs}`);
      if (!response.ok) return;
      const payload = await response.json();
      setManualBlacklist(
        Array.isArray(payload)
          ? payload
              .map(normalizeWhitelistEntry)
              .filter((entry): entry is WhitelistEntry => entry !== null)
          : []
      );
    } catch (requestError) {
      console.error("[BLACKLIST_FETCH_FAILED]", {
        message: requestError instanceof Error ? requestError.message : "未知錯誤",
      });
    }
  }, []);

  useEffect(() => {
    void loadManualBlacklist();
  }, [loadManualBlacklist]);

  const submitBlacklist = async () => {
    const url = input.trim();
    if (!url) return;
    if (hasMaliciousInput([url, blackTitle, blackReason])) {
      alert(MALICIOUS_INPUT_MESSAGE);
      return;
    }
    setBlackSaving(true);
    try {
      const response = await authFetch("/api/blacklist/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, title: blackTitle, reason: blackReason }),
      });
      if (!response.ok) throw new Error(await getErrorMessage(response));
      setInput("");
      setBlackTitle("");
      setBlackReason("");
      await loadManualBlacklist(blackSearch);
      alert("黑名單新增成功！之後爬到這個網域會直接歸檔為極高風險。");
    } catch (requestError) {
      alert(requestError instanceof Error ? requestError.message : "新增失敗");
    } finally {
      setBlackSaving(false);
    }
  };

  const removeManualBlacklist = async (entry: WhitelistEntry) => {
    if (!window.confirm(`確定要把 ${entry.url} 移出黑名單嗎？`)) return;
    try {
      const response = await authFetch(`/api/blacklist/${entry.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await getErrorMessage(response));
      await loadManualBlacklist(blackSearch);
    } catch (requestError) {
      alert(requestError instanceof Error ? requestError.message : "移除失敗");
    }
  };

  // 搜尋兩邊都要帶到——使用者不會知道一個網址是人工加的還是 AI 判的。
  const runPendingSearch = async () => {
    await loadBuckets(blackSearch, pendingSearch);
  };

  const runWhiteSearch = async () => {
    await loadWhitelist(whiteSearch);
  };

  const runBlackSearch = async () => {
    await Promise.all([loadManualBlacklist(blackSearch), loadBuckets(blackSearch, pendingSearch)]);
  };

  const reportFalsePositive = async (site: PendingWebsite) => {
    if (!site.id) {
      alert("這筆缺少識別碼，無法回報。");
      return;
    }
    const reason = window.prompt(
      `回報「${site.url}」為誤判？\n\n` +
        "確認後會把這個網域加入白名單，之後爬蟲遇到它會直接放行、不再判定。\n" +
        "（單純刪除沒有用——下次爬到還是會重新分析、重新出現。）\n\n" +
        "請輸入誤判原因：",
      "實際是合法網站"
    );
    if (reason === null) return;
    try {
      const qs = reason.trim() ? `?reason=${encodeURIComponent(reason.trim())}` : "";
      const response = await authFetch(
        `/api/crawler/result/${site.id}/false-positive/${qs}`,
        { method: "POST" }
      );
      if (!response.ok) throw new Error(await getErrorMessage(response));
      const result = (await response.json()) as { message?: string };
      onReviewed?.();
      await Promise.all([loadBuckets(blackSearch, pendingSearch), loadWhitelist(whiteSearch)]);
      alert(result.message ?? "已回報誤判");
    } catch (requestError) {
      alert(requestError instanceof Error ? requestError.message : "回報失敗");
    }
  };

  const selectableIds = remotePending
    .map((site) => site.id)
    .filter((id): id is number => typeof id === "number");
  const allSelected =
    selectableIds.length > 0 && selectedIds.length === selectableIds.length;

  const toggleOne = (id: number) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]);
  };

  const toggleAll = () => {
    setSelectedIds(allSelected ? [] : selectableIds);
  };

  // 清單一變（搜尋、換頁、覆核後重載），把已經不在畫面上的勾選清掉。
  // 不清的話會出現「已選 5 筆」但畫面上只看得到 2 筆的狀況——
  // 使用者按下批次確認時，等於確認了三筆他看不到的東西。
  useEffect(() => {
    setSelectedIds((prev) => {
      const visible = new Set(
        remotePending.map((site) => site.id).filter((id) => typeof id === "number"));
      const kept = prev.filter((id) => visible.has(id));
      return kept.length === prev.length ? prev : kept;
    });
  }, [remotePending]);

  const confirmSelectedAsBlacklist = async () => {
    if (selectedIds.length === 0) return;
    if (!window.confirm(
      `確認這 ${selectedIds.length} 筆都是毒品網站？\n\n` +
      "確認後會全部標成「極高風險」並留下一筆覆核紀錄。\n" +
      "要改回來需要管理員權限。"
    )) return;

    setBatchSaving(true);
    try {
      const response = await authFetch("/api/crawler/results/confirm-batch/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: selectedIds }),
      });
      if (!response.ok) throw new Error(await getErrorMessage(response));
      const result = (await response.json()) as { message?: string };
      setSelectedIds([]);
      onReviewed?.();
      // 帶著搜尋條件重載，不要把使用者剛才篩出來的結果洗掉。
      await loadBuckets(blackSearch, pendingSearch);
      alert(result.message ?? "批次確認完成");
    } catch (requestError) {
      alert(requestError instanceof Error ? requestError.message : "批次確認失敗");
    } finally {
      setBatchSaving(false);
    }
  };

  const confirmAsBlacklist = async (site: PendingWebsite) => {
    if (!site.id) {
      alert("這筆缺少識別碼，無法確認。");
      return;
    }
    // 兩顆覆核按鈕本來只有「回報誤判」會問（window.prompt 要輸入原因），
    // 「加入黑名單」按下去就直接生效。但後者其實比較嚴重——它把一個網站
    // 標成極高風險，而且按錯了要管理員才能改回來。兩邊的份量顛倒了。
    if (!window.confirm(
      `確認「${site.url}」是毒品網站？\n\n` +
      "確認後這筆會標成「極高風險」並留下覆核紀錄。\n" +
      "要改回來需要管理員權限。"
    )) return;
    try {
      const response = await authFetch(`/api/crawler/result/${site.id}/confirm/`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await getErrorMessage(response));
      onReviewed?.();
      await loadBuckets(blackSearch, pendingSearch);
    } catch (requestError) {
      alert(requestError instanceof Error ? requestError.message : "確認失敗");
    }
  };

  const loadBuckets = useCallback(async (blackKeyword = "", pendingKeyword = "") => {
    setBucketLoading(true);
    const toQuery = (keyword: string) =>
      keyword.trim() ? `&q=${encodeURIComponent(keyword.trim())}` : "";
    try {
      const [blackRes, pendingRes] = await Promise.all([
        authFetch(`/api/crawler/automated_24h_list/?bucket=blacklist&limit=200${toQuery(blackKeyword)}`),
        authFetch(`/api/crawler/automated_24h_list/?bucket=pending&limit=200${toQuery(pendingKeyword)}`),
      ]);
      if (blackRes.ok) {
        const payload = await blackRes.json();
        setRemoteBlacklist(normalizeSites(payload?.data));
        setRemoteBlacklistTotal(Number(payload?.total_count ?? 0));
      }
      if (pendingRes.ok) {
        const payload = await pendingRes.json();
        setRemotePending(normalizeSites(payload?.data));
        setRemotePendingTotal(Number(payload?.total_count ?? 0));
      }
    } catch (requestError) {
      console.error("[BUCKET_FETCH_FAILED]", {
        message: requestError instanceof Error ? requestError.message : "未知錯誤",
      });
    } finally {
      setBucketLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBuckets();
  }, [loadBuckets]);

  const addBlacklistItem = () => {
    const value = input.trim();
    if (!value) return;
    if (hasMaliciousInput([value])) {
      alert(MALICIOUS_INPUT_MESSAGE);
      return;
    }
    void submitBlacklist();
  };

  const loadWhitelist = useCallback(async (keyword = "") => {
    setWhitelistLoading(true);
    try {
      const qs = keyword.trim() ? `?q=${encodeURIComponent(keyword.trim())}` : "";
      const response = await authFetch(`/api/whitelist/${qs}`);
      if (!response.ok) throw new Error(await getErrorMessage(response));

      const payload = (await response.json()) as unknown;
      const list = Array.isArray(payload)
        ? payload
        : payload && typeof payload === "object" && Array.isArray((payload as { data?: unknown }).data)
          ? (payload as { data: unknown[] }).data
          : payload && typeof payload === "object" && Array.isArray((payload as { whitelist?: unknown }).whitelist)
            ? (payload as { whitelist: unknown[] }).whitelist
            : [];
      setWhitelistEntries(
        list
          .map(normalizeWhitelistEntry)
          .filter((entry): entry is WhitelistEntry => entry !== null)
      );
      setWhitelistError(null);
    } catch (requestError) {
      const message = requestError instanceof Error
        ? requestError.message
        : "未知錯誤";
      setWhitelistError(message);
      console.error("[WHITELIST_FETCH_FAILED]", {
        message,
        time: new Date().toISOString(),
      });
    } finally {
      setWhitelistLoading(false);
    }
  }, []);

  useEffect(() => {
    loadWhitelist();
  }, [loadWhitelist]);

  const addWhitelistEntry = async () => {
    const url = whiteUrl.trim();
    const title = whiteTitle.trim();
    const reason = whiteReason.trim();
    if (!url || !title || !reason) {
      alert("請完整填寫網址、標題與加入原因。");
      return;
    }
    if (hasMaliciousInput([url, title, reason])) {
      alert(MALICIOUS_INPUT_MESSAGE);
      return;
    }

    setWhitelistSaving(true);
    try {
      const response = await authFetch("/api/whitelist/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, title, reason, source: whiteSource }),
      });
      if (!response.ok) throw new Error(await getErrorMessage(response));

      const result = (await response.json()) as { message?: string };
      onReviewed?.();
      void loadBuckets(blackSearch, pendingSearch);
      setWhiteUrl("");
      setWhiteTitle("");
      setWhiteReason("");
      setWhiteSource("一般新增");
      await loadWhitelist(whiteSearch);
      alert(result.message || "白名單新增成功！");
    } catch (requestError) {
      const message = requestError instanceof Error
        ? requestError.message
        : "未知錯誤";
      alert(`白名單新增失敗：${message}`);
    } finally {
      setWhitelistSaving(false);
    }
  };

  const deleteWhitelistEntry = async (entry: WhitelistEntry) => {
    if (!confirm("確定要刪除這筆白名單嗎？刪除後該網址將重新接受爬蟲與 AI 檢測。")) {
      return;
    }

    try {
      const response = await authFetch(`/api/whitelist/${entry.id}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error(await getErrorMessage(response));

      await loadWhitelist(whiteSearch);
      alert("白名單刪除成功！");
    } catch (requestError) {
      const message = requestError instanceof Error
        ? requestError.message
        : "未知錯誤";
      alert(`白名單刪除失敗：${message}`);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#2B4C7E] to-[#1a2f4f] p-6">
      <div className="max-w-6xl mx-auto bg-white rounded-2xl shadow-xl p-8">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-2 text-gray-500 hover:text-blue-500 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          返回主頁
        </button>

        <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
          <div>
            <h1 className="text-2xl font-bold">網站名單管理</h1>
            <p className="text-sm text-gray-500 mt-1">
              AI 判定為可疑的網站，需由警員確認後才能正式分類。
            </p>
          </div>
          {remotePendingTotal > 0 && (
            <span className="bg-amber-100 text-amber-700 px-3 py-1.5 rounded-full text-sm font-medium">
              {remotePendingTotal} 筆待確認
            </span>
          )}
        </div>

        <div className="flex flex-wrap gap-3 mb-6 border-b pb-4">
          <button
            type="button"
            onClick={() => setTab("black")}
            className={`px-4 py-2 rounded-lg transition ${
              tab === "black" ? "bg-red-500 text-white" : "bg-gray-100 hover:bg-gray-200"
            }`}
          >
            黑名單（{remoteBlacklistTotal}）
          </button>
          <button
            type="button"
            onClick={() => setTab("white")}
            className={`px-4 py-2 rounded-lg transition ${
              tab === "white" ? "bg-green-500 text-white" : "bg-gray-100 hover:bg-gray-200"
            }`}
          >
            白名單（{whitelistEntries.length}）
          </button>
          <button
            type="button"
            onClick={() => setTab("pending")}
            className={`px-4 py-2 rounded-lg transition ${
              tab === "pending" ? "bg-amber-500 text-white" : "bg-gray-100 hover:bg-gray-200"
            }`}
          >
            待確認（{remotePendingTotal}）
          </button>
        </div>

        {tab === "black" && (
          <>
            <div className="border rounded-xl p-4 mb-5 bg-red-50/40">
              <p className="text-sm text-gray-600 mb-3">
                人工加入的黑名單。比對的是<strong>整個網域</strong>，
                之後爬到這個網域的任何頁面都會直接歸檔為極高風險，不再送 AI 判定。
              </p>
              <div className="flex flex-col sm:flex-row gap-2">
                <input
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") addBlacklistItem();
                  }}
                  className="border px-3 py-2.5 flex-1 rounded-lg focus:outline-none focus:ring-2 focus:ring-red-200"
                  placeholder="網址，例如 https://example.com/"
                />
                <input
                  value={blackTitle}
                  onChange={(event) => setBlackTitle(event.target.value)}
                  className="border px-3 py-2.5 sm:w-40 rounded-lg focus:outline-none focus:ring-2 focus:ring-red-200"
                  placeholder="名稱（選填）"
                />
                <input
                  value={blackReason}
                  onChange={(event) => setBlackReason(event.target.value)}
                  className="border px-3 py-2.5 sm:w-48 rounded-lg focus:outline-none focus:ring-2 focus:ring-red-200"
                  placeholder="原因（選填）"
                />
                <button
                  type="button"
                  onClick={addBlacklistItem}
                  disabled={blackSaving}
                  className="bg-red-500 hover:bg-red-600 disabled:opacity-50 text-white px-5 py-2.5 rounded-lg whitespace-nowrap"
                >
                  {blackSaving ? "新增中…" : "新增"}
                </button>
              </div>
            </div>

            <div className="flex gap-2 mb-4">
              <input
                value={blackSearch}
                onChange={(event) => setBlackSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void runBlackSearch();
                }}
                className="border px-3 py-2.5 flex-1 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200"
                placeholder="搜尋：網址、案件編號、名稱或原因"
              />
              <button
                type="button"
                onClick={() => void runBlackSearch()}
                className="bg-gray-700 hover:bg-gray-800 text-white px-5 py-2.5 rounded-lg"
              >
                搜尋
              </button>
              {blackSearch && (
                <button
                  type="button"
                  onClick={() => {
                    setBlackSearch("");
                    void loadManualBlacklist("");
                    void loadBuckets("", pendingSearch);
                  }}
                  className="border px-4 py-2.5 rounded-lg hover:bg-gray-50"
                >
                  清除
                </button>
              )}
            </div>

            <div className="mb-6">
              <h3 className="font-semibold text-gray-700 mb-2">
                人工加入（{manualBlacklist.length}）
              </h3>
              {manualBlacklist.length === 0 ? (
                <div className="text-center text-gray-400 py-6 border-2 border-dashed rounded-xl text-sm">
                  {blackSearch ? "沒有符合的黑名單" : "還沒有人工加入的黑名單"}
                </div>
              ) : (
                <div className="space-y-2">
                  {manualBlacklist.map((entry) => (
                    <div key={entry.id} className="flex items-center justify-between gap-3 border border-red-200 bg-red-50/40 p-4 rounded-xl">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <ShieldAlert className="text-red-500 shrink-0" size={18} />
                          <ExternalLink url={entry.url} className="break-all font-medium" />
                        </div>
                        <p className="text-sm text-gray-500 mt-1">
                          {entry.title || "未命名"}　原因：{entry.reason}　
                          由 {entry.addedBy} 於 {entry.createdAt} 加入
                        </p>
                      </div>
                      {/*
                        新增開放給一般人員，刪除只有管理員看得到。
                        刪黑名單等於把一個已確認的毒品網站取消標記，那是破壞性的方向。
                        後端一樣有擋（verify_admin），這裡只是不要顯示一個按了會失敗的按鈕。
                      */}
                      {userIsAdmin && (
                        <button
                          type="button"
                          onClick={() => void removeManualBlacklist(entry)}
                          className="border border-red-300 text-red-600 px-4 py-2 rounded-lg hover:bg-red-50 shrink-0"
                        >
                          移除
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <h3 className="font-semibold text-gray-700 mb-2">
              AI 判定為極高風險（{remoteBlacklistTotal}）
            </h3>

            <div className="space-y-3">
              {remoteBlacklist.length === 0 ? (
                <div className="text-center text-gray-400 py-12 border-2 border-dashed rounded-xl">
                  目前沒有資料
                </div>
              ) : (
                remoteBlacklist.map((item) => (
                  <div key={item.url} className="flex items-center justify-between gap-3 border p-4 rounded-xl">
                    <div className="flex items-center gap-3 min-w-0">
                      <ShieldAlert className="text-red-500 shrink-0" />
                      <ExternalLink url={item.url} className="break-all" />
                    </div>
                    <button
                      type="button"
                      onClick={() => void reportFalsePositive(item)}
                      className="border border-amber-400 text-amber-700 hover:bg-amber-50 px-4 py-2 rounded-lg shrink-0 whitespace-nowrap"
                      title="這不是毒品網站？回報誤判會把該網域加入白名單"
                    >
                      回報誤判
                    </button>
                  </div>
                ))
              )}
            </div>
          </>
        )}

        {tab === "white" && (
          <>
            <div className="mb-6 rounded-xl border border-green-200 bg-green-50/50 p-4">
              <h2 className="mb-3 font-bold text-gray-800">新增白名單</h2>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <input
                  value={whiteUrl}
                  onChange={(event) => setWhiteUrl(event.target.value)}
                  placeholder="網址"
                  className="rounded-lg border px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-green-200"
                />
                <input
                  value={whiteTitle}
                  onChange={(event) => setWhiteTitle(event.target.value)}
                  placeholder="標題"
                  className="rounded-lg border px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-green-200"
                />
                <input
                  value={whiteReason}
                  onChange={(event) => setWhiteReason(event.target.value)}
                  placeholder="加入原因"
                  className="rounded-lg border px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-green-200"
                />
              </div>
              <button
                type="button"
                onClick={addWhitelistEntry}
                disabled={whitelistSaving}
                className="mt-3 rounded-lg bg-green-500 px-5 py-2.5 text-white hover:bg-green-600 disabled:cursor-not-allowed disabled:bg-gray-300"
              >
                {whitelistSaving ? "新增中…" : "新增白名單"}
              </button>
              <p className="mt-2 text-xs text-gray-500">新增後該網域底下所有頁面都會跳過檢測。刪除僅限管理員。</p>
            </div>

            {whitelistError && (
              <div className="mb-5 rounded-lg border border-red-200 bg-red-50 p-3 text-red-700">
                無法取得白名單：{whitelistError}
              </div>
            )}

            <div className="flex gap-2 mb-4">
              <input
                value={whiteSearch}
                onChange={(event) => setWhiteSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void runWhiteSearch();
                }}
                className="border px-3 py-2.5 flex-1 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200"
                placeholder="搜尋：網址、名稱或原因"
              />
              <button
                type="button"
                onClick={() => void runWhiteSearch()}
                className="bg-gray-700 hover:bg-gray-800 text-white px-5 py-2.5 rounded-lg"
              >
                搜尋
              </button>
              {whiteSearch && (
                <button
                  type="button"
                  onClick={() => {
                    setWhiteSearch("");
                    void loadWhitelist("");
                  }}
                  className="border px-4 py-2.5 rounded-lg text-gray-600 hover:bg-gray-50"
                >
                  清除
                </button>
              )}
            </div>

            <div className="space-y-3">
              {whitelistLoading ? (
                <div className="py-12 text-center text-gray-400">正在取得白名單…</div>
              ) : whitelistEntries.length === 0 ? (
                <div className="text-center text-gray-400 py-12 border-2 border-dashed rounded-xl">
                  {whiteSearch ? "沒有符合的白名單" : "目前沒有白名單資料"}
                </div>
              ) : (
                (() => {
                  // 誤判回報與一般新增分開顯示。兩者意義不同：
                  // 「誤判回報」代表模型判錯過，那是改善模型的線索；
                  // 「一般新增」是我們主動排除的已知正常網站。
                  // 混在一起看就分不出模型到底錯在哪裡。
                  const groups: Array<[string, string, WhitelistEntry[]]> = [
                    ["誤判回報", "AI 判錯、由人工回報後加入的。這些是改善模型的線索。",
                     whitelistEntries.filter((e) => e.source === "誤判回報")],
                    ["一般新增", "主動排除的已知正常網站。",
                     whitelistEntries.filter((e) => e.source !== "誤判回報")],
                  ];
                  return groups
                    .filter(([, , rows]) => rows.length > 0)
                    .map(([label, hint, rows]) => (
                      <div key={label} className="mb-5">
                        <h3 className="font-semibold text-gray-700">
                          {label}（{rows.length}）
                        </h3>
                        <p className="mb-2 text-sm text-gray-500">{hint}</p>
                        <div className="space-y-2">
                          {rows.map((entry) => (
                  <div key={entry.id} className="flex flex-col justify-between gap-3 rounded-xl border p-4 sm:flex-row sm:items-center">
                    <div className="flex min-w-0 items-start gap-3">
                      <ShieldCheck className="mt-0.5 shrink-0 text-green-500" />
                      <div className="min-w-0">
                        <p className="font-medium text-gray-800">{entry.title}</p>
                        <p className="mt-1 break-all"><ExternalLink url={entry.url} className="text-blue-600" /></p>
                        <p className="mt-1 text-sm text-gray-500">
                          原因：{entry.reason}　由 {entry.addedBy} 於 {entry.createdAt} 加入
                        </p>
                      </div>
                    </div>
                    {userIsAdmin && (
                      <button
                        type="button"
                        onClick={() => deleteWhitelistEntry(entry)}
                        className="shrink-0 text-gray-500 hover:text-red-500"
                      >
                        刪除
                      </button>
                    )}
                  </div>
                          ))}
                        </div>
                      </div>
                    ));
                })()
              )}
            </div>
          </>
        )}

        {tab === "pending" && (
          <div className="space-y-4">
            <div className="flex gap-2 mb-4">
              <input
                value={pendingSearch}
                onChange={(event) => setPendingSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void runPendingSearch();
                }}
                className="border px-3 py-2.5 flex-1 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-200"
                placeholder="搜尋：網址或案件編號"
              />
              <button
                type="button"
                onClick={() => void runPendingSearch()}
                className="bg-gray-700 hover:bg-gray-800 text-white px-5 py-2.5 rounded-lg"
              >
                搜尋
              </button>
              {pendingSearch && (
                <button
                  type="button"
                  onClick={() => {
                    setPendingSearch("");
                    void loadBuckets(blackSearch, "");
                  }}
                  className="border px-4 py-2.5 rounded-lg text-gray-600 hover:bg-gray-50"
                >
                  清除
                </button>
              )}
            </div>
            {bucketLoading ? (
              <div className="text-center text-gray-400 py-12">載入中…</div>
            ) : remotePending.length === 0 ? (
              <div className="text-center py-14 border-2 border-dashed border-gray-200 rounded-xl">
                <Check className="w-10 h-10 text-green-500 mx-auto mb-3" />
                <p className="font-medium text-gray-700">
                  {pendingSearch ? "沒有符合的待確認網站" : "目前沒有待確認網站"}
                </p>
                <p className="text-sm text-gray-400 mt-1">
                  {pendingSearch ? "換個關鍵字，或按「清除」看全部。" : "AI 發現可疑網站後會自動出現在這裡。"}
                </p>
              </div>
            ) : (
              <>
              {/*
                批次動作列。只在有東西可選的時候出現，不要在空清單上放一排
                按不動的控制項。
              */}
              {selectableIds.length > 0 && (
                <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-white px-4 py-3">
                  <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      className="w-4 h-4 accent-red-500"
                    />
                    全選本頁（{selectableIds.length} 筆）
                  </label>

                  {selectedIds.length > 0 && (
                    <>
                      <span className="text-sm text-gray-500">
                        已選 <strong className="text-gray-800">{selectedIds.length}</strong> 筆
                      </span>
                      <button
                        type="button"
                        disabled={batchSaving}
                        onClick={() => void confirmSelectedAsBlacklist()}
                        className="bg-red-500 hover:bg-red-600 disabled:bg-gray-300 text-white px-4 py-2 rounded-lg text-sm ml-auto"
                      >
                        {batchSaving ? "處理中…" : `批次加入黑名單（${selectedIds.length}）`}
                      </button>
                      <button
                        type="button"
                        onClick={() => setSelectedIds([])}
                        className="border px-3 py-2 rounded-lg text-sm text-gray-600 hover:bg-gray-50"
                      >
                        取消選取
                      </button>
                    </>
                  )}
                </div>
              )}

              {remotePendingTotal > remotePending.length && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                  共 <strong>{remotePendingTotal}</strong> 筆待確認，
                  這裡顯示風險最高的 {remotePending.length} 筆。
                  處理完會自動補上後面的。
                </div>
              )}
              {remotePending.map((site) => (
                <div key={site.url} className="border border-amber-200 bg-amber-50/50 rounded-xl p-5">
                  <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                    <div className="flex min-w-0 gap-3">
                      {/* 沒有 id 的那幾筆不能覆核（後端用 id 定位），所以也不給勾 */}
                      {typeof site.id === "number" && (
                        <input
                          type="checkbox"
                          checked={selectedIds.includes(site.id)}
                          onChange={() => toggleOne(site.id as number)}
                          className="mt-1 w-4 h-4 shrink-0 accent-red-500"
                          aria-label={`選取 ${site.url}`}
                        />
                      )}
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2 mb-2">
                        <span className="bg-amber-500 text-white px-2.5 py-1 rounded-full text-xs font-medium">待確認</span>
                        <span className="text-red-600 font-bold">風險分數 {site.score}</span>
                        <span className="text-sm text-gray-500">{site.riskLevel}</span>
                      </div>
                      <p className="font-medium text-gray-800 break-all"><ExternalLink url={site.url} /></p>
                      <p className="text-xs text-gray-400 mt-2">辨識時間：{site.detectedAt}</p>
                    </div>
                    </div>
                    <div className="flex gap-2 shrink-0">
                      <button
                        type="button"
                        onClick={() => void confirmAsBlacklist(site)}
                        className="bg-red-500 hover:bg-red-600 text-white px-4 py-2.5 rounded-lg"
                      >
                        加入黑名單
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setWhiteUrl(site.url);
                          setWhiteTitle("");
                          setWhiteReason("經警員人工確認為誤判");
                          // 從待確認過來的＝AI 判過高風險又被人推翻，算誤判回報
                          setWhiteSource("誤判回報");
                          setTab("white");
                        }}
                        className="bg-green-500 hover:bg-green-600 text-white px-4 py-2.5 rounded-lg"
                      >
                        加入白名單
                      </button>
                    </div>
                  </div>
                </div>
              ))}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
