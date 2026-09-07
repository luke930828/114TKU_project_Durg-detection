/**
 * 風險等級的判定集中在這裡。
 *
 * risk_level 是給人看的字串（"中風險 (建議人工覆核)"），會隨判定規則調整而變動——
 * 2026-08-30 就從三級變成四級，中間多了「高風險 (優先人工覆核)」。
 *
 * 千萬不要用完整字串做等值比對。App.tsx 之前寫 riskLevel === "中風險"，
 * 但後端存的是 "中風險 (建議人工覆核)"，那個比對從來沒成功過，
 * pendingSites 一直是空的，而且沒有人發現。
 */

/** 兩個引擎都指向毒品，或文字引擎高度確定 —— 應該進黑名單 */
export const isHighRisk = (level: string | undefined | null): boolean => {
  const l = (level ?? "").trim();
  return l.startsWith("極高風險") || l.startsWith("高風險");
};

/** 需要人工看一眼（含高風險與中風險） */
export const needsReview = (level: string | undefined | null): boolean => {
  const l = (level ?? "").trim();
  return isHighRisk(l) || l.startsWith("中風險");
};

/** 只需要建議覆核、還不到進黑名單的程度 */
export const isMediumRisk = (level: string | undefined | null): boolean =>
  (level ?? "").trim().startsWith("中風險");
