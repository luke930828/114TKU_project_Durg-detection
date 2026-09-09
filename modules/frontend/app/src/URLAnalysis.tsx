import {
  ArrowLeft,
  Search,
  CheckCircle2,
  AlertTriangle,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { authFetch } from "./auth";
import { hasMaliciousInput, MALICIOUS_INPUT_MESSAGE } from "./inputSecurity";

interface URLAnalysisProps {
  onBack: () => void;
}

interface ImageSize {
  width: number;
  height: number;
}

interface RepresentativeDetection {
  className: string;
  confidence: number;
  coordinates: [number, number, number, number];
  format: "xyxy" | "xywh";
  normalized: boolean;
}

// OCR 不在前端顯示：圖片裡的文字直接送給 NLP 當額外的文字證據，影響的是風險
// 分數本身，不是多一個給人看的區塊。承辦人員要看的是「這一頁幾分、為什麼」，
// 不是一堆從包裝上讀到的碎片。原始結果仍存在 ai_analysis_results.ocr_results。

const clampCoordinate = (value: number) => Math.min(1, Math.max(0, value));

const isValidTargetUrl = (value: string) => {
  try {
    const parsedUrl = new URL(value);
    if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") {
      return false;
    }

    const hostname = parsedUrl.hostname;
    const isIpv4 = /^(\d{1,3}\.){3}\d{1,3}$/.test(hostname);
    const isIpv6 = hostname.includes(":");
    const hasValidHost =
      hostname === "localhost" || hostname.includes(".") || isIpv4 || isIpv6;

    return hasValidHost;
  } catch {
    return false;
  }
};

const normalizeRepresentativeDetections = (
  value: unknown
): RepresentativeDetection[] => {
  if (!Array.isArray(value)) return [];

  return value.flatMap((item): RepresentativeDetection[] => {
    if (!item || typeof item !== "object") return [];

    const detection = item as Record<string, unknown>;
    const usesXyxy = Array.isArray(detection.box);
    const rawCoordinates = usesXyxy ? detection.box : detection.bbox;
    if (!Array.isArray(rawCoordinates) || rawCoordinates.length !== 4) return [];

    const coordinates = rawCoordinates.map(Number);
    if (coordinates.some((coordinate) => !Number.isFinite(coordinate))) return [];

    const confidence = Number(detection.confidence);
    const classNameValue = detection.class_name ?? detection.class;

    return [{
      className: typeof classNameValue === "string" ? classNameValue : "未知類別",
      confidence: Number.isFinite(confidence) ? confidence : 0,
      coordinates: coordinates as [number, number, number, number],
      format: usesXyxy ? "xyxy" : "xywh",
      normalized: coordinates.every(
        (coordinate) => coordinate >= 0 && coordinate <= 1
      ),
    }];
  });
};

const getDetectionPosition = (
  detection: RepresentativeDetection,
  imageSize: ImageSize | null
) => {
  const [first, second, third, fourth] = detection.coordinates;
  let x1 = first;
  let y1 = second;
  let x2 = detection.format === "xywh" ? first + third : third;
  let y2 = detection.format === "xywh" ? second + fourth : fourth;

  if (!detection.normalized) {
    if (!imageSize?.width || !imageSize.height) return null;
    x1 /= imageSize.width;
    x2 /= imageSize.width;
    y1 /= imageSize.height;
    y2 /= imageSize.height;
  }

  x1 = clampCoordinate(x1);
  y1 = clampCoordinate(y1);
  x2 = clampCoordinate(x2);
  y2 = clampCoordinate(y2);

  if (x2 <= x1 || y2 <= y1) return null;
  return { x1, y1, x2, y2 };
};

export function URLAnalysis({ onBack }: URLAnalysisProps) {
  const [url, setUrl] = useState("");
  const [urlError, setUrlError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [serverMessage, setServerMessage] = useState<string | null>(null);
  const [analysisData, setAnalysisData] = useState<any | null>(null);
  const [isPolling, setIsPolling] = useState(false);
  const [representativeImageSize, setRepresentativeImageSize] = useState<ImageSize | null>(null);

  // 儲存輪詢計時器
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 儲存目前正在進行的 fetch，停止測試時可以取消
  const abortControllerRef = useRef<AbortController | null>(null);

  // 每次開始檢測都會產生新的編號
  // 使用者取消後，舊請求即使回傳也不會更新畫面
  const sessionIdRef = useRef(0);

  // 輪詢上限：20 秒一次、最多 21 次，大約七分鐘。
  // 以前沒有上限，遇到後端沒有寫回結果的紀錄就會一直轉，使用者只能自己重整；
  // 而且每次輪詢都會讓後端重新派一次爬蟲任務，等於一邊空轉一邊加重負擔。
  const POLL_INTERVAL_MS = 20000;
  const POLL_MAX_ATTEMPTS = 21;
  const pollAttemptRef = useRef(0);

  const BACKEND_PATH = "/api/scan_target/";

  const representativeImageBase64 = analysisData?.representative_image_base64;
  const representativeImageSrc =
    typeof representativeImageBase64 === "string" && representativeImageBase64.trim()
      ? `data:image/jpeg;base64,${representativeImageBase64.replace(/\s/g, "")}`
      : null;
  const representativeDetections = normalizeRepresentativeDetections(
    analysisData?.representative_image_detections
  );

  useEffect(() => {
    setRepresentativeImageSize(null);
  }, [representativeImageSrc]);


  // 停止輪詢
  const stopPolling = () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }

    setIsPolling(false);
  };

  // 取消目前正在進行的 API 請求
  const abortCurrentRequest = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  };

  // 使用者按下「停止測試」
  const handleCancelAnalysis = () => {
    // 讓目前所有舊請求失效
    sessionIdRef.current += 1;

    stopPolling();
    abortCurrentRequest();

    setLoading(false);
    setIsPolling(false);
    setAnalysisData(null);
    setServerMessage("使用者已停止本次網址分析。");

    console.log("使用者已停止網址分析。");
  };

  // 元件離開時清理請求與輪詢
  useEffect(() => {
    return () => {
      sessionIdRef.current += 1;
      stopPolling();
      abortCurrentRequest();
    };
  }, []);

  // 啟動每 20 秒輪詢
  const startPolling = (
    targetUrl: string,
    currentSessionId: number
  ) => {
    stopPolling();
    setIsPolling(true);
    pollAttemptRef.current = 0;

    pollingRef.current = setInterval(async () => {
      // 已經不是目前這一次分析，直接停止
      if (sessionIdRef.current !== currentSessionId) {
        stopPolling();
        return;
      }

      pollAttemptRef.current += 1;

      if (pollAttemptRef.current > POLL_MAX_ATTEMPTS) {
        stopPolling();
        abortCurrentRequest();
        setLoading(false);
        setServerMessage(
          "分析逾時：等待超過七分鐘仍未取得完整結果，已停止等待。" +
            "本次進度已存到後台，稍後可到「風險網址列表」查看，或重新分析一次。"
        );
        console.warn("輪詢已達上限，停止等待。");
        return;
      }

      console.log(
        `系統背景自動輪詢中（第 ${pollAttemptRef.current}/${POLL_MAX_ATTEMPTS} 次），檢查 YOLO 與 NLP 是否完成...`
      );

      const controller = new AbortController();
      abortControllerRef.current = controller;

      try {
        const checkRes = await authFetch(BACKEND_PATH, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            url: targetUrl,
          }),
          signal: controller.signal,
        });

        // 使用者已經取消，禁止更新畫面
        if (sessionIdRef.current !== currentSessionId) {
          return;
        }

        if (!checkRes.ok) {
          console.error(
            `輪詢失敗，HTTP 狀態碼：${checkRes.status}`
          );
          return;
        }

        const checkData = await checkRes.json();

        if (
          (checkData.status === "success" ||
            checkData.status === "safe") &&
          checkData.data
        ) {
          const incomingData = checkData.data;

          const isYoloPending =
            incomingData.yolo_details === "影像分析中...";

          const isNlpPending =
            incomingData.nlp_details === "文字分析中...";

          if (isYoloPending || isNlpPending) {
            setServerMessage(
              "AI 引擎正在同步交叉比對文本與影像特徵，請稍候..."
            );

            console.log(
              "仍有 AI 引擎正在分析，繼續輪詢..."
            );
          } else {
            setAnalysisData(incomingData);

            setServerMessage(
              "YOLO 與 NLP 雙引擎分析完成。"
            );

            setLoading(false);
            stopPolling();
            abortControllerRef.current = null;
          }
        } else if (checkData.status === "error") {
          stopPolling();
          setLoading(false);

          alert(
            `【後端處理錯誤】\n${
              checkData.message || "未知錯誤"
            }`
          );
        }
      } catch (error: any) {
        if (error.name === "AbortError") {
          console.log("目前輪詢請求已取消。");
          return;
        }

        console.error("輪詢時連線異常：", error);
      } finally {
        if (
          abortControllerRef.current === controller
        ) {
          abortControllerRef.current = null;
        }
      }
    }, POLL_INTERVAL_MS);
  };

  // 開始網址分析
  const handleAnalyze = async () => {
    const currentUrl = url.trim();

    if (!currentUrl || loading) {
      return;
    }

    if (hasMaliciousInput([currentUrl])) {
      setUrlError(MALICIOUS_INPUT_MESSAGE);
      setServerMessage(null);
      setAnalysisData(null);
      return;
    }

    if (!isValidTargetUrl(currentUrl)) {
      setUrlError("請輸入完整且有效的網址，例如：https://example.com");
      setServerMessage(null);
      setAnalysisData(null);
      return;
    }

    setUrlError(null);

    stopPolling();
    abortCurrentRequest();

    // 建立本次分析的識別編號
    sessionIdRef.current += 1;
    const currentSessionId = sessionIdRef.current;

    setLoading(true);
    setIsPolling(false);
    setServerMessage(
      "正在建立分析任務，請稍候..."
    );
    setAnalysisData(null);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const response = await authFetch(BACKEND_PATH, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          url: currentUrl,
        }),
        signal: controller.signal,
      });

      // 若使用者已經停止，不繼續處理回應
      if (sessionIdRef.current !== currentSessionId) {
        return;
      }

      let resData: any;

      try {
        resData = await response.json();
      } catch {
        throw new Error(
          `伺服器回應異常，HTTP 狀態碼：${response.status}`
        );
      }

      console.log("收到後端回應：", resData);

      if (
        resData.status === "success" ||
        resData.status === "safe"
      ) {
        if (!resData.data) {
          setLoading(false);
          setServerMessage(
            resData.message || "後端未回傳分析資料。"
          );
          return;
        }

        const incomingData = resData.data;

        const isYoloPending =
          incomingData.yolo_details === "影像分析中...";

        const isNlpPending =
          incomingData.nlp_details === "文字分析中...";

        if (isYoloPending || isNlpPending) {
          setServerMessage(
            "部分分析已完成，正在等待其他 AI 引擎完成..."
          );

          startPolling(
            currentUrl,
            currentSessionId
          );
        } else {
          setServerMessage(
            resData.message || "雙引擎分析完成。"
          );

          setAnalysisData(incomingData);
          setLoading(false);
        }
      } else if (resData.status === "processing") {
        setServerMessage(
          resData.message ||
            "系統正在進行背景分析..."
        );

        startPolling(
          currentUrl,
          currentSessionId
        );
      } else if (resData.status === "error") {
        alert(
          `【後端處理錯誤】\n${
            resData.message || "未知錯誤"
          }`
        );

        setLoading(false);
      } else {
        alert(
          `後端回傳非預期狀態：${resData.status}`
        );

        setLoading(false);
      }
    } catch (error: any) {
      if (error.name === "AbortError") {
        console.log("網址分析請求已取消。");
        return;
      }

      console.error("連線失敗：", error);

      alert(
        `【連線失敗】\n請確認 Tailscale 有開啟，且後端已啟動！\n錯誤原因：${error.message}`
      );

      setLoading(false);
    } finally {
      if (
        abortControllerRef.current === controller
      ) {
        abortControllerRef.current = null;
      }
    }
  };

  // 返回主頁前先停止所有工作
  const handleBackWithCleanup = () => {
    sessionIdRef.current += 1;

    stopPolling();
    abortCurrentRequest();

    onBack();
  };

  return (
    <div className="relative min-h-screen bg-[#1a2f4f] p-6 text-white flex flex-col items-center justify-center">
      {/* ========================= */}
      {/* 全螢幕分析中畫面 */}
      {/* ========================= */}
      {loading && (
        <div className="fixed inset-0 z-50 bg-[#122744]/95 backdrop-blur-md flex flex-col items-center justify-center px-6">
          {/* 旋轉圓圈 */}
          <div className="relative flex items-center justify-center">
            <div className="w-24 h-24 rounded-full border-[8px] border-cyan-300/20 border-t-cyan-400 animate-spin" />

            <div className="absolute w-12 h-12 rounded-full border-[5px] border-blue-300/20 border-b-blue-400 animate-spin [animation-direction:reverse] [animation-duration:1.2s]" />
          </div>

          <h2 className="mt-8 text-2xl font-bold text-blue-200 tracking-wide">
            正在進行多模態分析
          </h2>

          <p className="mt-3 text-sm text-gray-300 text-center max-w-md leading-relaxed">
            系統正在使用 NLP 與 YOLO
            分析目標網站的文字和影像特徵
          </p>

          <div className="mt-4 flex items-center gap-2 text-xs text-cyan-300">
            <span
              className="w-2 h-2 bg-cyan-400 rounded-full animate-bounce"
              style={{ animationDelay: "0ms" }}
            />

            <span
              className="w-2 h-2 bg-cyan-400 rounded-full animate-bounce"
              style={{ animationDelay: "150ms" }}
            />

            <span
              className="w-2 h-2 bg-cyan-400 rounded-full animate-bounce"
              style={{ animationDelay: "300ms" }}
            />
          </div>

          {serverMessage && (
            <p className="mt-5 max-w-lg text-center text-sm text-gray-300">
              {serverMessage}
            </p>
          )}

          <button
            type="button"
            onClick={handleCancelAnalysis}
            className="mt-9 min-w-40 bg-red-600 hover:bg-red-700 active:bg-red-800 px-7 py-3 rounded-xl flex items-center justify-center gap-2 font-bold text-white shadow-lg shadow-red-950/40 transition"
          >
            <XCircle size={20} />
            停止測試
          </button>

          <p className="mt-4 text-xs text-gray-500">
            {isPolling
              ? "系統正在背景追蹤分析進度"
              : "系統正在建立分析任務"}
          </p>
        </div>
      )}

      {/* ========================= */}
      {/* 網址輸入卡片 */}
      {/* ========================= */}
      <div className="max-w-4xl w-full sm:min-h-[360px] bg-white p-6 sm:p-10 rounded-2xl shadow-2xl border border-gray-200">
        <button
          type="button"
          onClick={handleBackWithCleanup}
          className="text-gray-500 flex items-center gap-2 hover:text-[#2B4C7E] transition mb-4 text-sm"
        >
          <ArrowLeft size={18} />
          返回主頁
        </button>

        <h1 className="text-xl font-bold mb-2 text-[#2B4C7E] tracking-wide">
          核心通道：網址多模態檢測
        </h1>

        <p className="text-xs text-gray-500 mb-6">
          輸入網址後，系統將自動比對歷史資料庫；若無紀錄則派發爬蟲與
          AI 進行即時分析。
        </p>

        <div className="mb-6">
          <div className="flex gap-2">
            <input
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
                if (urlError) setUrlError(null);
              }}
              disabled={loading}
              aria-invalid={Boolean(urlError)}
              aria-describedby={urlError ? "url-validation-error" : undefined}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !loading
                ) {
                  handleAnalyze();
                }
              }}
              placeholder="輸入目標網址，例如 https://littlehigh.com/cart"
              className={`flex-1 px-4 py-2 rounded bg-gray-50 text-gray-800 placeholder:text-gray-400 border focus:outline-none focus:ring-2 text-sm disabled:opacity-50 ${
                urlError
                  ? "border-red-500 focus:border-red-500 focus:ring-red-100"
                  : "border-gray-300 focus:border-blue-500 focus:ring-blue-100"
              }`}
            />

            {!loading ? (
              <button
                type="button"
                onClick={handleAnalyze}
                disabled={!url.trim()}
                className="bg-blue-500 hover:bg-blue-600 px-5 py-2 rounded flex items-center gap-2 font-medium transition disabled:bg-blue-900 disabled:text-gray-500 disabled:cursor-not-allowed text-sm"
              >
                <Search size={18} />
                開始檢測
              </button>
            ) : (
              <button
                type="button"
                onClick={handleCancelAnalysis}
                className="bg-red-600 hover:bg-red-700 px-5 py-2 rounded flex items-center gap-2 font-medium transition text-sm"
              >
                <XCircle size={18} />
                停止測試
              </button>
            )}
          </div>
          {urlError && (
            <p id="url-validation-error" className="mt-2 text-sm font-medium text-red-600">
              {urlError}
            </p>
          )}
        </div>

        {/* 分析完成或取消後的狀態提示 */}
        {serverMessage && !loading && (
          <div
            className={`p-4 rounded-xl flex gap-3 items-start animate-fadeIn ${
              analysisData
                ? "bg-blue-950/80 border-blue-600/40"
                : "bg-slate-950/70 border-slate-600/40"
            } border`}
          >
            <CheckCircle2
              className={`${
                analysisData
                  ? "text-blue-400"
                  : "text-gray-400"
              } shrink-0 mt-0.5`}
              size={20}
            />

            <div>
              <div
                className={`text-sm font-bold ${
                  analysisData
                    ? "text-blue-300"
                    : "text-gray-300"
                }`}
              >
                系統狀態更新
              </div>

              <p className="text-xs text-gray-300 mt-1 leading-relaxed">
                {serverMessage}
              </p>
            </div>
          </div>
        )}

        {/* ========================= */}
        {/* AI 分析報告 */}
        {/* ========================= */}
        {analysisData && (
          <div className="mt-6 bg-[#0f2342] p-6 rounded-xl border border-blue-800 shadow-inner animate-fadeIn">
            <div className="flex items-center gap-2 mb-4">
              <AlertTriangle
                className={
                  // 讀等級判色，不要在前端自己訂門檻。
                  // 原本寫死 74，後來門檻改過三次這行都沒跟著改。
                  (analysisData.risk_level || "").startsWith("極高")
                    ? "text-red-500"
                    : "text-yellow-500"
                }
                size={20}
              />

              <h3 className="text-lg font-bold text-white">
                AI 多模態分析報告
              </h3>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="bg-[#1e3a63] p-4 rounded-lg flex flex-col justify-center items-center">
                <span className="text-gray-400 text-xs mb-1">
                  文字分數
                </span>

                <span className="text-3xl font-black text-white">
                  {analysisData.nlp_score ?? analysisData.risk_score}
                </span>
                <span className="text-gray-400 text-xs mt-1">
                  影像 {analysisData.yolo_score ?? 0}
                </span>
              </div>

              <div className="bg-[#1e3a63] p-4 rounded-lg flex flex-col justify-center items-center">
                <span className="text-gray-400 text-xs mb-1">
                  風險判定等級
                </span>

                <span
                  className={`text-xl font-bold ${
                    analysisData.risk_level === "極高風險"
                      ? "text-red-500"
                      : analysisData.risk_level.startsWith("中風險")
                        ? "text-amber-500"
                        : "text-green-400"
                  }`}
                >
                  {analysisData.risk_level}
                </span>
              </div>

              <div className="col-span-2 bg-[#1e3a63] p-4 rounded-lg">
                <span className="text-blue-300 text-xs font-bold block mb-1">
                  文字特徵檢出（NLP）
                </span>

                <p className="text-sm text-gray-200">
                  {analysisData.nlp_details}
                </p>
              </div>

              <div className="col-span-2 bg-[#1e3a63] p-4 rounded-lg">
                <span className="text-blue-300 text-xs font-bold block mb-1">
                  影像特徵檢出（YOLO）
                </span>

                <p className="text-sm text-gray-200">
                  {analysisData.yolo_details}
                </p>

                <div className="mt-4 max-w-2xl">
                  {representativeImageSrc ? (
                    <div className="relative inline-block max-w-full overflow-hidden rounded-xl border border-blue-700/70 bg-[#0b1c35]">
                      <img
                        src={representativeImageSrc}
                        alt="YOLO 最高風險代表圖"
                        className="block h-auto max-w-full"
                        onLoad={(event) => {
                          setRepresentativeImageSize({
                            width: event.currentTarget.naturalWidth,
                            height: event.currentTarget.naturalHeight,
                          });
                        }}
                      />
                      {representativeDetections.map((detection, index) => {
                        const position = getDetectionPosition(
                          detection,
                          representativeImageSize
                        );
                        if (!position) return null;

                        const confidence = detection.confidence <= 1
                          ? detection.confidence * 100
                          : detection.confidence;

                        return (
                          <div
                            key={`${detection.className}-${index}`}
                            className="absolute border-2 border-cyan-400"
                            style={{
                              left: `${position.x1 * 100}%`,
                              top: `${position.y1 * 100}%`,
                              width: `${(position.x2 - position.x1) * 100}%`,
                              height: `${(position.y2 - position.y1) * 100}%`,
                            }}
                          >
                            <span className="absolute left-0 top-0 whitespace-nowrap rounded-br bg-cyan-400 px-1.5 py-0.5 text-xs font-bold text-[#082f49]">
                              {detection.className} {Math.round(confidence)}%
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="aspect-video rounded-xl border-2 border-dashed border-blue-700/70 bg-[#0b1c35] flex items-center justify-center px-6 text-center text-sm text-gray-400">
                      沒有圖可顯示
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
