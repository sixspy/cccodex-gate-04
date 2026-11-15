import { useEffect, useState } from "react";

const METHOD_OPTIONS = [
  { value: "reject", label: "整条剔除" },
  { value: "rewrite", label: "改写为未分类事件" },
];

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || data.conflicts?.[0]?.reason || "请求没有完成");
    error.data = data;
    throw error;
  }
  return data;
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [dictionary, setDictionary] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [savingCode, setSavingCode] = useState("");

  async function openDay(bizDate) {
    const [nextDetail, nextDict] = await Promise.all([
      request(`/api/dates/${bizDate}`),
      request(`/api/dates/${bizDate}/dict`),
    ]);
    setDetail(nextDetail);
    setDictionary(nextDict);
    setDrafts({});
  }

  async function refreshDates() {
    const rows = await request("/api/dates");
    setDates(rows);
    return rows;
  }

  async function refresh(prefer) {
    const rows = await refreshDates();
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) await openDay(target);
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function saveMethod(code) {
    const method = drafts[code];
    if (!detail || !method) return;
    setSavingCode(code);
    setFormError("");
    setNotice("");
    try {
      const result = await request(`/api/dates/${detail.biz_date}/dict`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: [{ code, method }] }),
      });
      setDictionary(result.overview);
      setDrafts((current) => {
        const next = { ...current };
        delete next[code];
        return next;
      });
      setNotice(
        result.applied.length
          ? `事件码 ${code} 的清洗办法已落定：${result.applied[0].method_name}。本日不再改口。`
          : ""
      );
      await refreshDates();
    } catch (error) {
      setFormError(error.message);
      if (error.data?.overview) setDictionary(error.data.overview);
    } finally {
      setSavingCode("");
    }
  }

  async function buildDay() {
    if (!detail) return;
    if (!dictionary) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      // 收成请求带上字典设置里的清洗办法，与设置保持一致。
      const cleanMethods = Object.fromEntries(
        dictionary.unknown.filter((row) => row.locked).map((row) => [row.code, row.method])
      );
      await request(`/api/dates/${detail.biz_date}/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clean_methods: cleanMethods }),
      });
      await openDay(detail.biz_date);
      await refreshDates();
      setNotice("明细层已按字典收成。");
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function pickDay(bizDate) {
    setFormError("");
    setNotice("");
    try {
      await openDay(bizDate);
    } catch (error) {
      setFormError(error.message);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setDictionary(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。只有字典认得出的事件才收进明细。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  const writer = profile.role === "writer";
  const unknownRows = dictionary?.unknown || [];
  const pendingUnknown = unknownRows.filter((row) => !row.locked);
  const canBuild = Boolean(detail) && Boolean(dictionary) && pendingUnknown.length === 0 && !savingCode;

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => pickDay(row.biz_date)}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.built ? "明细已收" : "尚未构建"}</em>
              <span>
                贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条
                {row.dict_count ? ` · 已定办法 ${row.dict_count} 码` : ""}
              </span>
            </button>
          ))}
        </aside>
        {detail ? (
          <section className="sheet">
            <div className="split-head">
              <h2>{detail.biz_date}</h2>
              <button disabled={!writer || busy || !canBuild} onClick={buildDay}>
                {pendingUnknown.length ? "先定清洗办法" : "按字典构建明细层"}
              </button>
            </div>
            <p className="note">
              IN 进闸，OUT 出闸，RS 倒箱。箱号要是四位字母加七位数字。字典认不出的未知事件码，收成前先定清洗办法。
            </p>
            {!writer ? <p className="note">核对员不能定字典设置，也不能重新构建。</p> : null}
            {formError ? <p className="warn">{formError}</p> : null}
            {notice ? <p className="ok">{notice}</p> : null}
            {savingCode ? <p className="note">事件码 {savingCode} 的清洗办法还在保存，这一码暂不能收成。</p> : null}
            {pendingUnknown.length && writer ? (
              <p className="note">
                还有未知事件码没定清洗办法：{pendingUnknown.map((row) => row.code).join("、")}。定完才能收成。
              </p>
            ) : null}

            <section className="dict">
              <div className="split-head">
                <h3>字典设置</h3>
                <span className="note">未知码的清洗办法落定后，本日禁止改口</span>
              </div>
              <div className="dict-groups">
                <article>
                  <h4>已登记事件</h4>
                  <ul className="tags">
                    {dictionary?.registered.map((row) => (
                      <li key={row.code} className="tag known">
                        <code>{row.code}</code>
                        <span>{row.name}</span>
                      </li>
                    ))}
                  </ul>
                  <p className="note">已登记事件直接按字典进明细，不需要定清洗办法。</p>
                </article>

                <article>
                  <h4>未知事件</h4>
                  {unknownRows.length ? (
                    <table>
                      <thead>
                        <tr>
                          <th>未知码</th>
                          <th>扫描</th>
                          <th>剔除条数</th>
                          <th>改写条数</th>
                          <th>办法名</th>
                          <th>清洗办法</th>
                        </tr>
                      </thead>
                      <tbody>
                        {unknownRows.map((row) => (
                          <tr key={row.code}>
                            <td><code>{row.code}</code></td>
                            <td>{row.scan_count}</td>
                            <td>{row.rejected_count}</td>
                            <td>{row.rewritten_count}</td>
                            <td>{row.locked ? row.method_name : "未定"}</td>
                            <td>
                              {row.locked ? (
                                <span className="locked">本日已定 · 禁止改口</span>
                              ) : writer ? (
                                <span className="inline">
                                  <select
                                    value={drafts[row.code] || ""}
                                    onChange={(event) =>
                                      setDrafts((current) => ({ ...current, [row.code]: event.target.value }))
                                    }
                                  >
                                    <option value="">选清洗办法</option>
                                    {METHOD_OPTIONS.map((option) => (
                                      <option key={option.value} value={option.value}>{option.label}</option>
                                    ))}
                                  </select>
                                  <button
                                    className="small"
                                    disabled={!drafts[row.code] || savingCode === row.code}
                                    onClick={() => saveMethod(row.code)}
                                  >
                                    {savingCode === row.code ? "保存中" : "写进字典"}
                                  </button>
                                </span>
                              ) : (
                                <span className="note">未定</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <p className="note">这一天的扫描事件码都在字典里。</p>
                  )}
                  <p className="note">
                    剔除条数是这一码没进明细的扫描（整条剔除、清洗不符、箱号或时间坏掉）；改写条数是改写成「未分类事件」后进明细的扫描。办法名只在字典设置落定后才有。
                  </p>
                </article>
              </div>

              <h4>清洗详情</h4>
              {dictionary?.attempts.length ? (
                <table>
                  <thead>
                    <tr><th>未知码</th><th>办法名</th><th>操作人</th><th>结果</th><th>说明</th><th>时间</th></tr>
                  </thead>
                  <tbody>
                    {dictionary.attempts.map((row, index) => (
                      <tr key={index}>
                        <td><code>{row.code}</code></td>
                        <td>{row.method_name || "—"}</td>
                        <td>{row.actor}</td>
                        <td>{row.outcome_label}</td>
                        <td>{row.note}</td>
                        <td>{row.at}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="note">还没有人给这一天的未知码定清洗办法。</p>
              )}
            </section>

            <div className="layers">
              <article>
                <h3>贴源层</h3>
                <ul>
                  {detail.ods.map((row) => (
                    <li key={row.id}>
                      <span>{row.terminal_code}</span>
                      <code>{JSON.stringify(row.raw_payload)}</code>
                    </li>
                  ))}
                </ul>
              </article>
              <article>
                <h3>明细层</h3>
                {detail.moves.length ? (
                  <table>
                    <thead>
                      <tr><th>来源</th><th>原事件码</th><th>收成后事件</th><th>箱号</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      {detail.moves.map((row) => (
                        <tr key={`${row.source_id}-${row.event_kind}`}>
                          <td>{row.source_id}</td>
                          <td><code>{row.raw_event_code || "—"}</code></td>
                          <td className={row.raw_event_code && !["IN", "OUT", "RS"].includes(row.raw_event_code) ? "rewritten" : ""}>
                            {row.event_kind}
                          </td>
                          <td>{row.container_no}</td>
                          <td>{row.event_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p>这一天的明细还是空的。</p>}
                <h3>剔除</h3>
                <ul>
                  {detail.rejects.map((row) => (
                    <li key={row.source_id}>
                      <span>来源 {row.source_id}{row.event_code ? ` · 原事件码 ${row.event_code}` : ""}</span>
                      <em>{row.reason}</em>
                    </li>
                  ))}
                  {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                </ul>
              </article>
            </div>
          </section>
        ) : null}
      </main>
    </>
  );
}
