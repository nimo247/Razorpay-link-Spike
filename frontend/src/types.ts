export type InvoiceStatus =
  | "OVERDUE"
  | "PARTIALLY_PAID"
  | "DISPUTED"
  | "PAID"
  | "HUMAN_REVIEW";

export type PromiseStatus =
  | "PROPOSED"
  | "VALIDATED"
  | "LINK_CREATED"
  | "PAID"
  | "BROKEN"
  | "HUMAN_REVIEW";

export interface Invoice {
  id: string;
  customer_name: string;
  original_amount_paise: number;
  paid_amount_paise: number;
  disputed_amount_paise: number;
  outstanding_amount_paise: number;
  due_date: string;
  status: InvoiceStatus;
  created_at: string;
  updated_at: string;
}

export interface PaymentPromise {
  id: string;
  invoice_id: string;
  customer_message: string;
  promised_amount_paise: number;
  disputed_amount_paise: number;
  promised_date: string;
  evidence_quotes: string[];
  status: PromiseStatus;
  payment_link_id: string | null;
  payment_link_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEvent {
  id: string;
  invoice_id: string;
  promise_id: string | null;
  event_type: string;
  event_data: Record<string, unknown>;
  created_at: string;
}

export interface InvoiceWorkspace {
  invoice: Invoice;
  promise: PaymentPromise | null;
  audit_events: AuditEvent[];
}

export interface EvidenceSpan {
  quote: string;
  start: number;
  end: number;
}

export interface ExtractionPreview {
  intent: string;
  promised_amount_paise: number | null;
  disputed_amount_paise: number;
  promised_date_text: string | null;
  evidence_quotes: string[];
  needs_review: boolean;
  review_reason: string | null;
  ready_for_validation: boolean;
  resolved_promised_date: string | null;
  validation_errors: string[];
  evidence_spans: EvidenceSpan[];
}

export interface PromiseCreateInput {
  customer_message: string;
  promised_amount_paise: number;
  disputed_amount_paise: number;
  promised_date: string;
  evidence_quotes: string[];
}

export interface PaymentLinkCreation {
  promise_id: string;
  invoice_id: string;
  promise_status: PromiseStatus;
  payment_link_id: string;
  payment_link_url: string;
  amount_paise: number;
  reused: boolean;
}