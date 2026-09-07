/**
 * 可疑網址的超連結。
 *
 * 為什麼不直接寫 <a href={url}>
 * ──────────────────────────────
 * 這裡顯示的網址全部來自爬蟲，也就是「不受信任的外部輸入」。直接放進 href
 * 有兩個問題：
 *
 * 1. javascript: / data: 開頭的網址點下去會在「我們自己的頁面上」執行程式碼。
 *    爬蟲抓到的網址理論上都是 http(s)，但這裡是最後一道防線——只放行
 *    http 與 https，其餘一律退回純文字，不做成連結。
 *
 * 2. rel="noreferrer" 不只是慣例，對這個系統是實質需求：沒有它的話，
 *    對方的伺服器日誌會看到 Referer 是我們的系統位址，等於告訴一個
 *    販毒網站「有執法單位在看你」。noopener 則是避免對方的頁面透過
 *    window.opener 反過來操作我們這一頁。
 *
 * target="_blank" 讓承辦人員點開查證時不會離開手上的清單。
 */
type Props = {
  url: string;
  className?: string;
  title?: string;
};

const SAFE_SCHEMES = ["http:", "https:"];

export function isLinkableUrl(url: string): boolean {
  if (!url) return false;
  try {
    return SAFE_SCHEMES.includes(new URL(url).protocol);
  } catch {
    // 解析不出來就不是我們能安全開啟的東西
    return false;
  }
}

export function ExternalLink({ url, className = "", title }: Props) {
  if (!isLinkableUrl(url)) {
    // 不是 http(s) 就純文字顯示。承辦人員仍然看得到內容，只是不能點。
    return <span className={className}>{url}</span>;
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer nofollow"
      title={title ?? `在新分頁開啟 ${url}`}
      className={`${className} underline decoration-dotted underline-offset-2 hover:decoration-solid`}
      // 點連結不要觸發外層的展開／選取
      onClick={(event) => event.stopPropagation()}
    >
      {url}
    </a>
  );
}
