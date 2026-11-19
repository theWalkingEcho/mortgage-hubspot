"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type WebhookLog = {
  id: number;
  event_id: string;
  borrower_name: string;
  loan_amount: number;
  referral_partner: string;
  hubspot_deal_id: string | null;
  status: "PROCESSING" | "SUCCESS" | "FAILED";
  error_message: string | null;
  created_at: string;
};

type LeadForm = {
  borrower_name: string;
  email: string;
  phone: string;
  loan_amount: string;
  property_state: string;
  referral_partner: string;
};

const initialForm: LeadForm = {
  borrower_name: "",
  email: "",
  phone: "",
  loan_amount: "",
  property_state: "NSW",
  referral_partner: "Direct",
};

const configuredApiBase = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/$/, "");
const apiBase =
  configuredApiBase ||
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : "");

export default function OpsDashboard() {
  const [logs, setLogs] = useState<WebhookLog[]>([]);
  const [form, setForm] = useState(initialForm);
  const [apiKey, setApiKey] = useState("");
  const [username, setUsername] = useState("admin");
  const [loginKey, setLoginKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  const request = useCallback(
    async (path: string, init?: RequestInit) => {
      if (!apiBase) throw new Error("Set NEXT_PUBLIC_API_URL in the frontend deployment.");
      let response: Response;
      try {
        response = await fetch(`${apiBase}${path}`, {
          ...init,
          headers: {
            ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
            "Content-Type": "application/json",
            ...(init?.headers || {}),
          },
        });
      } catch {
        throw new Error(
          `Could not connect to the API at ${apiBase}. Check that the backend is running and accessible.`,
        );
      }
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.detail || `Request failed (${response.status})`);
      }
      return result;
    },
    [apiKey],
  );

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      await request("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, api_key: loginKey }),
      });
      setApiKey(loginKey);
      setLoginKey("");
      setMessage("Signed in successfully.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Sign-in failed.");
    } finally {
      setLoading(false);
    }
  }

  const fetchLogs = useCallback(async () => {
    const data = (await request("/api/v1/logs")) as WebhookLog[];
    setLogs(data);
  }, [request]);

  useEffect(() => {
    if (!apiKey) return;
    let active = true;
    const refresh = async () => {
      try {
        if (active) await fetchLogs();
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : "Could not load logs.");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 8000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [apiKey, fetchLogs]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const eventId = crypto.randomUUID();
      const result = await request("/api/v1/ingest-lead", {
        method: "POST",
        body: JSON.stringify({
          ...form,
          event_id: eventId,
          loan_amount: form.loan_amount,
        }),
      });
      setMessage(
        result.status === "duplicate_ignored"
          ? "This event was already handled."
          : `Lead synced. HubSpot deal ID: ${result.deal_id}`,
      );
      setForm(initialForm);
      await fetchLogs();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Lead sync failed.");
      try {
        await fetchLogs();
      } catch {
        // Keep the original submission error visible.
      }
    } finally {
      setLoading(false);
    }
  }

  async function retry(eventId: string) {
    setMessage(`Retrying event ${eventId}…`);
    try {
      const result = await request(
        `/api/v1/logs/${encodeURIComponent(eventId)}/retry`,
        { method: "POST" },
      );
      setMessage(`Retry finished: ${result.status}.`);
      await fetchLogs();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Retry failed.");
      try {
        await fetchLogs();
      } catch {
        // Keep the retry error visible if refreshing also fails.
      }
    }
  }

  function handleLogout() {
    setApiKey("");
    setLogs([]);
    setMessage("");
  }

  return (
    <main className="shell">
      <header className="page-header">
        <div>
          <p className="eyebrow">INTERNAL OPERATIONS</p>
          <h1>Mortgage CRM Sync Monitor</h1>
          <p className="lede">
            Submit a demo lead and monitor its contact, deal, and audit-log sync.
          </p>
        </div>
        {apiKey && (
          <button className="secondary-button" onClick={handleLogout}>
            Sign out
          </button>
        )}
      </header>

      <p className="notice" role="status" aria-live="polite">
        {message}
      </p>

      {!apiKey ? (
        <section className="panel login-panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">ADMIN ACCESS</p>
              <h2>Sign in to the dashboard</h2>
            </div>
          </div>
          <form className="login-form" onSubmit={handleLogin}>
            <label>
              Admin username
              <input
                required
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
            <label>
              Admin API key
              <input
                required
                type="password"
                autoComplete="current-password"
                placeholder="Enter ADMIN_API_KEY"
                value={loginKey}
                onChange={(event) => setLoginKey(event.target.value)}
              />
            </label>
            <button className="primary-button" disabled={loading}>
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </section>
      ) : (
      <>
      <section className="panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">LEAD INTAKE</p>
            <h2>Simulate a broker referral</h2>
          </div>
          <span className="privacy-note">Demo data only — avoid real borrower details.</span>
        </div>
        <form className="lead-form" onSubmit={handleSubmit}>
          <label>
            Borrower name
            <input
              required
              maxLength={200}
              value={form.borrower_name}
              onChange={(event) =>
                setForm({ ...form, borrower_name: event.target.value })
              }
            />
          </label>
          <label>
            Email
            <input
              required
              type="email"
              value={form.email}
              onChange={(event) => setForm({ ...form, email: event.target.value })}
            />
          </label>
          <label>
            Phone
            <input
              type="tel"
              value={form.phone}
              onChange={(event) => setForm({ ...form, phone: event.target.value })}
            />
          </label>
          <label>
            Loan amount (AUD)
            <input
              required
              type="number"
              min="1"
              step="0.01"
              value={form.loan_amount}
              onChange={(event) =>
                setForm({ ...form, loan_amount: event.target.value })
              }
            />
          </label>
          <label>
            Property state
            <select
              value={form.property_state}
              onChange={(event) =>
                setForm({ ...form, property_state: event.target.value })
              }
            >
              {["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"].map(
                (state) => (
                  <option key={state}>{state}</option>
                ),
              )}
            </select>
          </label>
          <label>
            Referral partner
            <input
              maxLength={200}
              value={form.referral_partner}
              onChange={(event) =>
                setForm({ ...form, referral_partner: event.target.value })
              }
            />
          </label>
          <button className="primary-button submit-button" disabled={loading || !apiKey}>
            {loading ? "Syncing…" : "Submit lead and sync to HubSpot"}
          </button>
        </form>
      </section>

      <section className="panel log-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">AUDIT TRAIL</p>
            <h2>Recent lead events</h2>
          </div>
          <button
            className="secondary-button"
            disabled={!apiKey}
            onClick={() => void fetchLogs().catch((error: Error) => setMessage(error.message))}
          >
            Refresh
          </button>
        </div>
        {!apiKey ? (
          <p className="empty-state">Enter the dashboard API key to load the audit log.</p>
        ) : logs.length === 0 ? (
          <p className="empty-state">No lead events yet.</p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Borrower</th>
                  <th>Loan (AUD)</th>
                  <th>Referral partner</th>
                  <th>HubSpot deal</th>
                  <th>Event</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <tr key={log.id}>
                    <td>
                      <span className={`status status-${log.status.toLowerCase()}`}>
                        {log.status}
                      </span>
                      {log.error_message && (
                        <p className="error-text">{log.error_message}</p>
                      )}
                    </td>
                    <td>{log.borrower_name}</td>
                    <td>
                      {Number(log.loan_amount).toLocaleString("en-AU", {
                        style: "currency",
                        currency: "AUD",
                        maximumFractionDigits: 0,
                      })}
                    </td>
                    <td>{log.referral_partner}</td>
                    <td className="mono">{log.hubspot_deal_id || "—"}</td>
                    <td className="mono">{log.event_id}</td>
                    <td>
                      {log.status === "FAILED" && (
                        <button
                          className="text-button"
                          onClick={() => void retry(log.event_id)}
                        >
                          Retry
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <footer>Internal demo dashboard · Refreshes every 8 seconds</footer>
      </>
      )}
    </main>
  );
}
