import { useEffect, useState } from "react";
import { AIDetection } from "./AIDetection";
import { URLAnalysis } from "./URLAnalysis";
import { Report } from "./Report";
import WebsiteQuery from "./WebsiteQuery";
import Login from "./Login";
import UserManagement from "./UserManagement";
import {
  AUTH_UNAUTHORIZED_EVENT,
  authFetch,
  clearAuthToken,
  getAuthToken,
  isAdmin,
} from "./auth";

export default function App() {
  const [page, setPage] = useState("home");
  // 黑名單／待確認／白名單都不再是前端狀態——WebsiteQuery 直接查後端。
  // 以前這裡的初始值是寫死的假資料（dark-market-x.onion / google.com），
  // 而且只有開過「AI 偵測」才會被填入、只填那一頁 50 筆，重新整理就歸零，
  // 所以「待確認 11 筆」從來不是待辦總量。

  const [isAuthenticated, setIsAuthenticated] = useState(
    Boolean(getAuthToken())
  );
  // 登入／登出時才會變，所以跟著 isAuthenticated 重算就夠了。
  const userIsAdmin = isAuthenticated && isAdmin();

  const handleLogout = async () => {
    const shouldLogout = window.confirm("確定要登出系統嗎？");
    if (!shouldLogout) return;

    // 先通知後端留一筆稽核紀錄。就算這步失敗（網路斷了、後端掛了）也一定要
    // 讓使用者登出，所以不擋在前面。
    try {
      await authFetch("/api/logout/", { method: "POST" });
    } catch {
      // 記不到就算了，不影響登出本身
    }

    clearAuthToken();
    setIsAuthenticated(false);
    setPage("home");
  };

  useEffect(() => {
    const handleUnauthorized = () => {
      setIsAuthenticated(false);
      setPage("home");
    };

    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    };
  }, []);

  if (!isAuthenticated) {
    return <Login onLogin={() => setIsAuthenticated(true)} />;
  }
  if (page === "ai")
    return (
      <AIDetection
        onBack={() => setPage("home")}
      />
    );

  if (page === "url") return <URLAnalysis onBack={() => setPage("home")} />;


  if (page === "report")
    return (
      <Report
        onBack={() => setPage("home")}
      />
    );

  if (page === "query")
    return (
      <WebsiteQuery
        onBack={() => setPage("home")}
      />
    );

  if (page === "users")
    return (
      <UserManagement
        onBack={() => setPage("home")}
        onUnauthorized={handleLogout}
      />
    );

  const cardStyle: React.CSSProperties = {
    backgroundColor: "#ffffff",
    borderRadius: "24px",
    padding: "40px 24px",
    textAlign: "center",
    cursor: "pointer",
    boxShadow: "0 8px 20px rgba(0,0,0,0.15)",
    minHeight: "220px",
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
    transition: "transform 0.2s ease, box-shadow 0.2s ease",
  };

  const titleStyle: React.CSSProperties = {
    fontSize: "22px",
    fontWeight: 700,
    color: "#2B4C7E",
    marginBottom: "12px",
  };

  const descStyle: React.CSSProperties = {
    fontSize: "15px",
    color: "#666666",
    lineHeight: 1.6,
    margin: 0,
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "linear-gradient(135deg, #2B4C7E 0%, #1A2F4F 100%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "40px 20px",
        boxSizing: "border-box",
      }}
    >
      <button
        type="button"
        onClick={handleLogout}
        style={{
          position: "absolute",
          top: "24px",
          right: "28px",
          background: "rgba(255,255,255,0.12)",
          border: "1px solid rgba(255,255,255,0.35)",
          borderRadius: "10px",
          color: "white",
          padding: "9px 16px",
          cursor: "pointer",
        }}
      >
        登出
      </button>
      <h1
        style={{
          color: "#ffffff",
          fontSize: "64px",
          fontWeight: 700,
          margin: "0 0 12px 0",
          textAlign: "center",
        }}
      >
        多模態毒品交易防制系統
      </h1>

      <p
        style={{
          color: "rgba(255,255,255,0.85)",
          fontSize: "20px",
          margin: "20px 0 40px 0",
          textAlign: "center",
        }}
      >
        選擇以下功能
      </p>

      <div
        style={{
          width: "100%",
          maxWidth: "1200px",
          display: "grid",
          gridTemplateColumns: "repeat(2, 1fr)",
          gap: "28px",
        }}
      >
        <div
          onClick={() => setPage("ai")}
          style={cardStyle}
          onMouseEnter={(e) => {
            e.currentTarget.style.transform = "translateY(-4px)";
            e.currentTarget.style.boxShadow = "0 12px 28px rgba(0,0,0,0.2)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "translateY(0)";
            e.currentTarget.style.boxShadow = "0 8px 20px rgba(0,0,0,0.15)";
          }}
        >
          <div style={titleStyle}>24小時AI自動識別</div>
          <p style={descStyle}>使用AI技術自動識別可疑內容</p>
        </div>

        <div
          onClick={() => setPage("url")}
          style={cardStyle}
          onMouseEnter={(e) => {
            e.currentTarget.style.transform = "translateY(-4px)";
            e.currentTarget.style.boxShadow = "0 12px 28px rgba(0,0,0,0.2)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "translateY(0)";
            e.currentTarget.style.boxShadow = "0 8px 20px rgba(0,0,0,0.15)";
          }}
        >
          <div style={titleStyle}>輸入網址識別</div>
          <p style={descStyle}>輸入網址進行識別分析</p>
        </div>

        <div
          onClick={() => setPage("report")}
          style={cardStyle}
          onMouseEnter={(e) => {
            e.currentTarget.style.transform = "translateY(-4px)";
            e.currentTarget.style.boxShadow = "0 12px 28px rgba(0,0,0,0.2)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "translateY(0)";
            e.currentTarget.style.boxShadow = "0 8px 20px rgba(0,0,0,0.15)";
          }}
        >
          <div style={titleStyle}>合併報表</div>
          <p style={descStyle}>彙整多筆資料並生成報表</p>
        </div>

        <div
          onClick={() => setPage("query")}
          style={cardStyle}
          onMouseEnter={(e) => {
            e.currentTarget.style.transform = "translateY(-4px)";
            e.currentTarget.style.boxShadow = "0 12px 28px rgba(0,0,0,0.2)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.transform = "translateY(0)";
            e.currentTarget.style.boxShadow = "0 8px 20px rgba(0,0,0,0.15)";
          }}
        >
          <div style={titleStyle}>查詢已識別網站</div>
          <p style={descStyle}>查詢與管理已標記的可疑網站資料庫</p>
        </div>

        {/*
          人員管理只有系統管理員看得到。

          之前這張卡是顯示給所有人的，但後端的 /api/users/ 掛的是 verify_admin，
          一般人員點進去只會拿到 403、停在錯誤畫面。看得到卻永遠進不去，
          比一開始就不顯示更讓人困惑——會以為是系統壞了。

          ⚠️ 這是使用者體驗，不是安全機制。localStorage 裡的角色使用者自己就能改，
             改了也只是讓自己多看到一張卡，點下去照樣被後端擋掉。
        */}
        {userIsAdmin && (
          <div
            onClick={() => setPage("users")}
            style={{ ...cardStyle, gridColumn: "1 / -1" }}
            onMouseEnter={(event) => {
              event.currentTarget.style.transform = "translateY(-4px)";
              event.currentTarget.style.boxShadow = "0 12px 28px rgba(0,0,0,0.2)";
            }}
            onMouseLeave={(event) => {
              event.currentTarget.style.transform = "translateY(0)";
              event.currentTarget.style.boxShadow = "0 8px 20px rgba(0,0,0,0.15)";
            }}
          >
            <div style={titleStyle}>人員與權限管理</div>
            <p style={descStyle}>新增人員、調整權限及管理帳號狀態</p>
          </div>
        )}
      </div>

      <p
        style={{
          color: "rgba(255,255,255,0.5)",
          fontSize: "14px",
          marginTop: "36px",
        }}
      >
        多模態毒品交易防制系統 ｜ 僅供執法單位使用
      </p>
    </div>
  );
}
