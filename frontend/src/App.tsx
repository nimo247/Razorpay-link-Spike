import { useEffect, useMemo, useState } from "react";

import "./App.css";
import {
  getInvoices,
  getInvoiceWorkspace,
} from "./api";
import type {
  AuditEvent,
  Invoice,
  InvoiceStatus,
  InvoiceWorkspace,
} from "./types";

const statusLabels: Record<InvoiceStatus, string> = {
  OVERDUE: "Overdue",
  PARTIALLY_PAID: "Partially paid",
  DISPUTED: "Disputed",
  PAID: "Paid",
  HUMAN_REVIEW: "Human review",
};

function formatMoney(amountPaise: number): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(amountPaise / 100);
}

function formatDate(value: string): string {
  return new Date(`${value}T00:00:00`).toLocaleDateString(
    "en-IN",
    {
      day: "numeric",
      month: "short",
      year: "numeric",
    },
  );
}

function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusClass(status: string): string {
  return status.toLowerCase().replaceAll("_", "-");
}

function eventTitle(eventType: string): string {
  return eventType
    .toLowerCase()
    .split("_")
    .map(
      (word) =>
        word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

function eventSummary(event: AuditEvent): string {
  const amount =
    event.event_data.amount_paid_paise ??
    event.event_data.promised_amount_paise ??
    event.event_data.amount_paise;

  if (typeof amount === "number") {
    return formatMoney(amount);
  }

  if (
    typeof event.event_data.status === "string"
  ) {
    return event.event_data.status.replaceAll("_", " ");
  }

  return "Recorded in audit ledger";
}

function App() {
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [selectedInvoiceId, setSelectedInvoiceId] =
    useState<string | null>(null);
  const [workspace, setWorkspace] =
    useState<InvoiceWorkspace | null>(null);
  const [loadingInvoices, setLoadingInvoices] =
    useState(true);
  const [loadingWorkspace, setLoadingWorkspace] =
    useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadInvoices() {
    try {
      setLoadingInvoices(true);
      setError(null);

      const result = await getInvoices();

      setInvoices(result);

      setSelectedInvoiceId((current) => {
        if (
          current &&
          result.some((invoice) => invoice.id === current)
        ) {
          return current;
        }

        return result[0]?.id ?? null;
      });
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not load invoices",
      );
    } finally {
      setLoadingInvoices(false);
    }
  }

  useEffect(() => {
  let active = true;

  async function loadInitialInvoices() {
    try {
      const result = await getInvoices();

      if (!active) {
        return;
      }

      setInvoices(result);
      setSelectedInvoiceId(result[0]?.id ?? null);
    } catch (requestError) {
      if (active) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "Could not load invoices",
        );
      }
    } finally {
      if (active) {
        setLoadingInvoices(false);
      }
    }
  }

  void loadInitialInvoices();

  return () => {
    active = false;
  };
}, []);

  useEffect(() => {
    if (!selectedInvoiceId) {
      return;
    }

    let active = true;

    async function loadWorkspace() {
      try {
        setLoadingWorkspace(true);
        setError(null);

        const result = await getInvoiceWorkspace(
          selectedInvoiceId as string,
        );

        if (active) {
          setWorkspace(result);
        }
      } catch (requestError) {
        if (active) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "Could not load recovery workspace",
          );
        }
      } finally {
        if (active) {
          setLoadingWorkspace(false);
        }
      }
    }

    void loadWorkspace();

    return () => {
      active = false;
    };
  }, [selectedInvoiceId]);

  const totals = useMemo(() => {
    return invoices.reduce(
      (summary, invoice) => {
        summary.original += invoice.original_amount_paise;
        summary.recovered += invoice.paid_amount_paise;
        summary.outstanding +=
          invoice.outstanding_amount_paise;
        summary.disputed += invoice.disputed_amount_paise;

        return summary;
      },
      {
        original: 0,
        recovered: 0,
        outstanding: 0,
        disputed: 0,
      },
    );
  }, [invoices]);

  const selectedInvoice = invoices.find(
    (invoice) => invoice.id === selectedInvoiceId,
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <span className="product-mark">PTP</span>
          <span className="product-name">Recovery Desk</span>
        </div>

        <div className="environment">
          <span className="environment-dot" />
          Razorpay Test Mode
        </div>
      </header>

      <main className="page">
        <section className="page-heading">
          <div>
            <p className="eyebrow">Accounts receivable</p>
            <h1>Promise-to-pay recovery</h1>
            <p className="page-description">
              Track customer commitments, payment outcomes,
              disputes, and every automated decision.
            </p>
          </div>

          <button
            className="secondary-button"
            onClick={() => void loadInvoices()}
            disabled={loadingInvoices}
          >
            {loadingInvoices ? "Refreshing…" : "Refresh data"}
          </button>
        </section>

        {error && (
          <div className="error-banner" role="alert">
            <strong>Unable to load data.</strong>
            <span>{error}</span>
          </div>
        )}

        <section className="summary-strip">
          <div className="summary-item">
            <span>Total receivables</span>
            <strong>{formatMoney(totals.original)}</strong>
          </div>

          <div className="summary-item recovered">
            <span>Recovered</span>
            <strong>{formatMoney(totals.recovered)}</strong>
          </div>

          <div className="summary-item">
            <span>Outstanding</span>
            <strong>{formatMoney(totals.outstanding)}</strong>
          </div>

          <div className="summary-item disputed">
            <span>Under dispute</span>
            <strong>{formatMoney(totals.disputed)}</strong>
          </div>
        </section>

        <div className="workspace-layout">
          <section className="invoice-panel">
            <div className="panel-heading">
              <div>
                <p className="section-label">Recovery queue</p>
                <h2>Invoices</h2>
              </div>

              <span className="record-count">
                {invoices.length} records
              </span>
            </div>

            {loadingInvoices && invoices.length === 0 ? (
              <div className="empty-state">Loading invoices…</div>
            ) : invoices.length === 0 ? (
              <div className="empty-state">
                No invoices in the recovery queue.
              </div>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Merchant</th>
                      <th>Outstanding</th>
                      <th>Due date</th>
                      <th>Status</th>
                    </tr>
                  </thead>

                  <tbody>
                    {invoices.map((invoice) => (
                      <tr
                        key={invoice.id}
                        className={
                          invoice.id === selectedInvoiceId
                            ? "selected-row"
                            : ""
                        }
                      >
                        <td>
                          <button
                            className="merchant-button"
                            onClick={() =>
                              setSelectedInvoiceId(invoice.id)
                            }
                          >
                            <strong>
                              {invoice.customer_name}
                            </strong>
                            <span>
                              {invoice.id.slice(0, 8)}
                            </span>
                          </button>
                        </td>

                        <td className="money-cell">
                          {formatMoney(
                            invoice.outstanding_amount_paise,
                          )}
                        </td>

                        <td>{formatDate(invoice.due_date)}</td>

                        <td>
                          <span
                            className={`status status--${statusClass(
                              invoice.status,
                            )}`}
                          >
                            {statusLabels[invoice.status]}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <aside className="detail-panel">
            {loadingWorkspace ? (
              <div className="empty-state">
                Loading recovery record…
              </div>
            ) : !workspace || !selectedInvoice ? (
              <div className="empty-state">
                Select an invoice to inspect its recovery record.
              </div>
            ) : (
              <>
                <div className="detail-heading">
                  <div>
                    <p className="section-label">
                      Selected account
                    </p>
                    <h2>{selectedInvoice.customer_name}</h2>
                    <p className="invoice-reference">
                      Invoice {selectedInvoice.id.slice(0, 8)}
                    </p>
                  </div>

                  <span
                    className={`status status--${statusClass(
                      selectedInvoice.status,
                    )}`}
                  >
                    {statusLabels[selectedInvoice.status]}
                  </span>
                </div>

                <div className="balance-grid">
                  <div>
                    <span>Original</span>
                    <strong>
                      {formatMoney(
                        selectedInvoice.original_amount_paise,
                      )}
                    </strong>
                  </div>

                  <div>
                    <span>Recovered</span>
                    <strong className="positive-value">
                      {formatMoney(
                        selectedInvoice.paid_amount_paise,
                      )}
                    </strong>
                  </div>

                  <div>
                    <span>Outstanding</span>
                    <strong>
                      {formatMoney(
                        selectedInvoice.outstanding_amount_paise,
                      )}
                    </strong>
                  </div>
                </div>

                {workspace.promise ? (
                  <section className="detail-section">
                    <div className="section-heading-row">
                      <h3>Customer commitment</h3>

                      <span
                        className={`status status--${statusClass(
                          workspace.promise.status,
                        )}`}
                      >
                        {workspace.promise.status.replaceAll(
                          "_",
                          " ",
                        )}
                      </span>
                    </div>

                    <blockquote>
                      “{workspace.promise.customer_message}”
                    </blockquote>

                    <div className="evidence-list">
                      {workspace.promise.evidence_quotes.map(
                        (quote) => (
                          <span key={quote}>{quote}</span>
                        ),
                      )}
                    </div>

                    <dl className="promise-details">
                      <div>
                        <dt>Promised amount</dt>
                        <dd>
                          {formatMoney(
                            workspace.promise
                              .promised_amount_paise,
                          )}
                        </dd>
                      </div>

                      <div>
                        <dt>Promise date</dt>
                        <dd>
                          {formatDate(
                            workspace.promise.promised_date,
                          )}
                        </dd>
                      </div>

                      <div>
                        <dt>Disputed</dt>
                        <dd>
                          {formatMoney(
                            workspace.promise
                              .disputed_amount_paise,
                          )}
                        </dd>
                      </div>
                    </dl>

                    {workspace.promise.payment_link_url && (
                      <a
                        className="payment-link"
                        href={
                          workspace.promise.payment_link_url
                        }
                        target="_blank"
                        rel="noreferrer"
                      >
                        View Razorpay payment link
                      </a>
                    )}
                  </section>
                ) : (
                  <section className="detail-section">
                    <h3>Customer commitment</h3>
                    <p className="muted">
                      No validated payment promise exists for
                      this invoice.
                    </p>
                  </section>
                )}

                <section className="detail-section audit-section">
                  <div className="section-heading-row">
                    <h3>Audit trail</h3>
                    <span className="record-count">
                      {workspace.audit_events.length} events
                    </span>
                  </div>

                  <ol className="timeline">
                    {workspace.audit_events.map((event) => (
                      <li key={event.id}>
                        <span className="timeline-marker" />

                        <div>
                          <div className="timeline-heading">
                            <strong>
                              {eventTitle(event.event_type)}
                            </strong>
                            <time>
                              {formatTimestamp(event.created_at)}
                            </time>
                          </div>

                          <p>{eventSummary(event)}</p>
                        </div>
                      </li>
                    ))}
                  </ol>
                </section>
              </>
            )}
          </aside>
        </div>
      </main>
    </div>
  );
}

export default App;