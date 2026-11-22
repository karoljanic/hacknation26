import { AdminDashboard } from "../../components/AdminDashboard";
import "./admin.css";

export const metadata = {
    title: "Panel administratora i analityka – Prawdowid",
};

export default function AdminPage() {
    return (
        <main id="main" className="container main">
            <nav aria-label="Ścieżka nawigacji" className="breadcrumbs">
                <ol>
                    <li>
                        <a href="/">Prawdowid</a>
                    </li>
                    <li aria-current="page">Panel administratora</li>
                </ol>
            </nav>

            <h1>Panel administratora i analityka</h1>

            <AdminDashboard />
        </main>
    );
}