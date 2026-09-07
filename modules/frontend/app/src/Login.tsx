import { useState, type FormEvent } from "react";
import { LockKeyhole } from "lucide-react";
import {
  API_BASE_URL,
  getErrorMessage,
  saveAuthToken,
  saveIdentity,
} from "./auth";
import { hasMaliciousInput, MALICIOUS_INPUT_MESSAGE } from "./inputSecurity";

interface Props {
  onLogin: () => void;
}

export default function Login({ onLogin }: Props) {
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!account.trim() || !password) return;
    if (hasMaliciousInput([account, password])) {
      setError(MALICIOUS_INPUT_MESSAGE);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/login/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account: account.trim(), password }),
      });

      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }

      const data = (await response.json()) as {
        access_token?: string; account?: string; role?: string;
      };
      if (!data.access_token) throw new Error("後端未回傳 access_token");

      saveAuthToken(data.access_token);
      // 角色只用來決定畫面上要顯示哪些功能，真正的權限由後端把關。
      saveIdentity(data.account ?? "", data.role ?? "");
      onLogin();
    } catch (requestError) {
      const message = requestError instanceof Error
        ? requestError.message
        : "未知錯誤";
      console.error("[AUTH_LOGIN_FAILED]", {
        message,
        time: new Date().toISOString(),
      });
      setError(`登入失敗：${message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#2B4C7E] to-[#1A2F4F] flex items-center justify-center p-6">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md bg-white rounded-3xl p-8 sm:p-10 shadow-2xl"
      >
        <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-[#2B4C7E] text-white flex items-center justify-center">
          <LockKeyhole size={28} />
        </div>
        <h1 className="text-2xl font-bold text-center text-[#2B4C7E]">
          系統登入
        </h1>
        <p className="text-sm text-gray-500 text-center mt-2 mb-7">
          多模態毒品交易防制系統
        </p>

        {error && (
          <div className="mb-5 p-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
            {error}
          </div>
        )}

        <label className="block text-sm font-medium text-gray-700 mb-2">
          帳號
        </label>
        <input
          value={account}
          onChange={(event) => setAccount(event.target.value)}
          autoComplete="username"
          className="w-full border border-gray-300 rounded-lg px-4 py-3 mb-5 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          placeholder="請輸入帳號"
        />

        <label className="block text-sm font-medium text-gray-700 mb-2">
          密碼
        </label>
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          className="w-full border border-gray-300 rounded-lg px-4 py-3 mb-7 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          placeholder="請輸入密碼"
        />

        <button
          type="submit"
          disabled={loading || !account.trim() || !password}
          className="w-full bg-[#2B4C7E] hover:bg-[#1A2F4F] disabled:bg-gray-300 text-white font-bold py-3 rounded-xl transition"
        >
          {loading ? "登入中…" : "登入"}
        </button>
      </form>
    </div>
  );
}
