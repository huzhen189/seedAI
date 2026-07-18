"use client";

import { useEffect, useRef, useState } from "react";

type Model = { id: string; label: string };
type Msg = { role: "user" | "ai"; content: string };
type AuthMode = "login" | "register";

const TOKEN_KEY = "seedai_token";

export default function Page() {
  const [token, setToken] = useState<string>("");
  const [models, setModels] = useState<Model[]>([]);
  const [modelId, setModelId] = useState<string>("deepseek");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [html, setHtml] = useState<string>("");
  // auth
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [authTip, setAuthTip] = useState("");

  const logRef = useRef<HTMLDivElement>(null);

  // 恢复 token
  useEffect(() => {
    const t = localStorage.getItem(TOKEN_KEY) || "";
    if (t) setToken(t);
  }, []);

  // 拉模型列表
  useEffect(() => {
    if (!token) return;
    fetch("/api/models", { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => (r.ok ? r.json() : []))
      .then((list: Model[]) => {
        setModels(list);
        if (list.length && !list.find((m) => m.id === modelId)) {
          setModelId(list[0].id);
        }
      })
      .catch(() => {});
  }, [token, modelId]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [messages]);

  function logout() {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
  }

  async function doAuth() {
    setAuthTip("");
    const url = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
    const body =
      authMode === "login"
        ? { username, password }
        : { username, password, email: email || undefined };
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      setAuthTip(e.detail || "认证失败");
      return;
    }
    const data = await res.json();
    localStorage.setItem(TOKEN_KEY, data.access_token);
    setToken(data.access_token);
  }

  async function send() {
    const text = input.trim();
    if (!text || busy || !token) return;
    setBusy(true);
    setInput("");
    const next: Msg[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setMessages((m) => [...m, { role: "ai", content: "" }]);

    try {
      const res = await fetch("/api/generate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          model_id: modelId,
          messages: [{ role: "user", content: text }],
        }),
      });
      if (!res.ok) {
        setAuthTip("生成失败(可能 token 过期,请重新登录)");
        setBusy(false);
        return;
      }
      // 解析 SSE
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let acc = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        let ev = "";
        for (const line of lines) {
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) {
            const data = line.slice(5).trim();
            if (ev === "token" && data !== "[DONE]") {
              acc += data;
              const cur = acc;
              setMessages((m) => {
                const copy = [...m];
                copy[copy.length - 1] = { role: "ai", content: cur };
                return copy;
              });
            }
          }
        }
      }
      setHtml(acc);
    } catch (e) {
      setAuthTip("生成异常:" + String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <div className="auth-wrap">
        <h2>{authMode === "login" ? "登录 SeedAI" : "注册 SeedAI"}</h2>
        <div className="tip">{authTip}</div>
        <input
          placeholder="用户名"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <input
          type="password"
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {authMode === "register" && (
          <input
            placeholder="邮箱(可选)"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        )}
        <button style={{ width: "100%", padding: "10px" }} onClick={doAuth}>
          {authMode === "login" ? "登录" : "注册"}
        </button>
        <div
          className="auth-switch"
          onClick={() => {
            setAuthMode(authMode === "login" ? "register" : "login");
            setAuthTip("");
          }}
        >
          {authMode === "login" ? "没有账号?去注册" : "已有账号?去登录"}
        </div>
      </div>
    );
  }

  return (
    <div className="container">
      <div className="pane-chat">
        <div className="chat-head">
          <h1>SeedAI</h1>
          <select value={modelId} onChange={(e) => setModelId(e.target.value)}>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
          <button style={{ background: "#333", padding: "6px 10px" }} onClick={logout}>
            退出
          </button>
        </div>
        <div className="chat-log" ref={logRef}>
          {messages.length === 0 && (
            <div className="msg ai">你好,描述你想做的网站,我来生成并实时预览。</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.content || "生成中…"}
            </div>
          ))}
        </div>
        <div className="chat-input">
          <textarea
            placeholder="例如:做一个个人作品集首页,深色风格,带导航和项目卡片"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button onClick={send} disabled={busy}>
            {busy ? "生成中" : "发送"}
          </button>
        </div>
      </div>
      <div className="pane-preview">
        <div className="preview-head">实时预览(iframe srcdoc)</div>
        <iframe className="preview-frame" srcDoc={html} title="preview" />
      </div>
    </div>
  );
}
