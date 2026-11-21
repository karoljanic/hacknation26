import type { ReactNode } from "react";

export const metadata = {
  title: "Prawdowid",
  description: "Narzędzie do weryfikacji informacji",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="pl">
      <body>{children}</body>
    </html>
  );
}
