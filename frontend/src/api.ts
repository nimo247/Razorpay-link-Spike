import type {
  Invoice,
  InvoiceWorkspace,
} from "./types";

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: {
      Accept: "application/json",
    },
  });

  if (!response.ok) {
    let message = `Request failed with HTTP ${response.status}`;

    try {
      const body = await response.json();

      if (typeof body.detail === "string") {
        message = body.detail;
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