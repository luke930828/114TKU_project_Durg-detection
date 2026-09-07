// 呼叫端一律傳 "/api/..."，交給 nginx.conf 的 /api proxy_pass 轉給 backend。
// 這樣前端完全不用知道 backend 在哪台機器，local/tailscale 切換不用重 build。
// （舊版曾經寫死一個 tailnet 位址，那其實是「前端自己」的位址、不是 backend 的，
// 而且那個 port 早就不用了。不在註解裡留下實際 IP，免得 make check 誤判成設定值）
export const API_BASE_URL = "";
export const TOKEN_STORAGE_KEY = "my_jwt_token";
export const AUTH_UNAUTHORIZED_EVENT = "auth:unauthorized";
export const ROLE_STORAGE_KEY = "my_role";
export const ACCOUNT_STORAGE_KEY = "my_account";

export const getAuthToken = () => localStorage.getItem(TOKEN_STORAGE_KEY);

export const saveAuthToken = (token: string) => {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
};

export const clearAuthToken = () => {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  localStorage.removeItem(ROLE_STORAGE_KEY);
  localStorage.removeItem(ACCOUNT_STORAGE_KEY);
};

// 登入時後端會回傳自己的角色，存起來給畫面決定要不要顯示管理員專屬的功能。
//
// ⚠️ 這只用來決定「畫面上要不要出現」。真正的權限由後端的 verify_admin 把關——
//    這裡的值使用者自己就能改（localStorage 誰都動得到），改了也只是讓自己看到
//    一個按鈕，按下去照樣 403。不要把任何安全判斷建立在這上面。
export const ADMIN_ROLE = "系統管理員";

export const getUserRole = () => localStorage.getItem(ROLE_STORAGE_KEY) ?? "";
export const getUserAccount = () => localStorage.getItem(ACCOUNT_STORAGE_KEY) ?? "";
export const isAdmin = () => getUserRole() === ADMIN_ROLE;

export const saveIdentity = (account: string, role: string) => {
  if (account) localStorage.setItem(ACCOUNT_STORAGE_KEY, account);
  if (role) localStorage.setItem(ROLE_STORAGE_KEY, role);
};

export const getErrorMessage = async (response: Response) => {
  try {
    const data = (await response.json()) as { detail?: string; message?: string };
    return data.detail || data.message || `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
};

const notifyUnauthorized = () => {
  clearAuthToken();
  window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
};

export const authFetch = async (path: string, options: RequestInit = {}) => {
  const token = getAuthToken();
  if (!token) {
    notifyUnauthorized();
    throw new Error("AUTH_TOKEN_MISSING");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...options.headers,
      "x-token": token,
    },
  });

  if (response.status === 401) {
    notifyUnauthorized();
  }

  return response;
};
