import type {
  ExtractionPreview,
  Invoice,
  InvoiceWorkspace,
  PaymentLinkCreation,
  PaymentPromise,
  PromiseCreateInput,
} from "./types";

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed with HTTP ${response.status}`;

    try {
      const body = await response.json();

      if (typeof body.detail === "string") {
        message = body.detail;
      } else if (body.detail) {
        message = JSON.stringify(body.detail);
      }
    } catch {
      // Keep the HTTP status message.
    }

    throw new Error(message);
  }

  return response.json() as Promise<T>;
}

export function getInvoices(): Promise<Invoice[]> {
  return request<Invoice[]>("/invoices");
}

export function getInvoiceWorkspace(
  invoiceId: string,
): Promise<InvoiceWorkspace> {
  return request<InvoiceWorkspace>(
    `/invoices/${invoiceId}/workspace`,
  );
}

export function extractPromise(
  invoiceId: string,
  customerMessage: string,
): Promise<ExtractionPreview> {
  return request<ExtractionPreview>(
    `/invoices/${invoiceId}/extract-promise`,
    {
      method: "POST",
      body: JSON.stringify({
        customer_message: customerMessage,
        message_timestamp: new Date().toISOString(),
      }),
    },
  );
}

export function createValidatedPromise(
  invoiceId: string,
  input: PromiseCreateInput,
): Promise<PaymentPromise> {
  return request<PaymentPromise>(
    `/invoices/${invoiceId}/promises`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function createPromisePaymentLink(
  promiseId: string,
): Promise<PaymentLinkCreation> {
  return request<PaymentLinkCreation>(
    `/promises/${promiseId}/payment-link`,
    {
      method: "POST",
    },
  );
}