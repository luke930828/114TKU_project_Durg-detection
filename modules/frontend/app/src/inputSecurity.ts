export const MALICIOUS_INPUT_MESSAGE =
  "偵測到疑似惡意程式碼或攻擊指令，已阻止送出。";

const MALICIOUS_PATTERNS: RegExp[] = [
  /<\s*\/?\s*(?:script|iframe|object|embed|svg|math|style|link|meta)\b/i,
  /<[^>]+\bon[a-z]+\s*=/i,
  /(?:javascript|vbscript)\s*:/i,
  /data\s*:\s*text\/html/i,
  /(?:'|"|`)\s*(?:or|and)\s+(?:'[^']*'|"[^"]*"|\d+)\s*=\s*(?:'[^']*'|"[^"]*"|\d+)/i,
  /\bunion\s+(?:all\s+)?select\b/i,
  /;\s*(?:drop|truncate|alter|delete|insert|update)\s+(?:table|database|from|into)\b/i,
  /\b(?:sleep|benchmark)\s*\(/i,
  /(?:--|#|\/\*)\s*(?:$|select|union|drop|insert|update|delete)/i,
  /(?:;|&&|\|\|)\s*(?:rm|curl|wget|shutdown|reboot|kill|nc|bash|sh)\b/i,
];

export const containsMaliciousInput = (value: string) =>
  MALICIOUS_PATTERNS.some((pattern) => pattern.test(value));

export const hasMaliciousInput = (values: string[]) =>
  values.some(containsMaliciousInput);

export const PASSWORD_REQUIREMENTS =
  "密碼至少需要 8 碼，並包含英文大寫、英文小寫、數字及特殊符號。";

export const getPasswordValidationMessage = (password: string) => {
  const missing: string[] = [];

  if (password.length < 8) missing.push("至少 8 碼");
  if (!/[A-Z]/.test(password)) missing.push("英文大寫");
  if (!/[a-z]/.test(password)) missing.push("英文小寫");
  if (!/\d/.test(password)) missing.push("數字");
  if (!/[^A-Za-z0-9\s]/.test(password)) missing.push("特殊符號");

  return missing.length > 0
    ? `密碼格式不符，請加入：${missing.join("、")}。`
    : null;
};
