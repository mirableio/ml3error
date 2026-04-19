let currentFilter = "unresolved";
let allItems = [];
// fps the user has expanded — kept across renders so switching filters
// or toggling resolve doesn't collapse the rows they were reading.
const expanded = new Set();

function fmtTs(ts) {
    if (!ts) return "–";
    const d = new Date(ts * 1000);
    return d.toLocaleString();
}
function fmtRel(ts) {
    if (!ts) return "–";
    const diff = (Date.now() / 1000) - ts;
    if (diff < 60) return Math.floor(diff) + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return Math.floor(diff / 86400) + "d ago";
}

async function fetchList() {
    const params = new URLSearchParams();
    if (currentFilter === "unresolved") params.set("resolved", "0");
    else if (currentFilter === "resolved") params.set("resolved", "1");
    const resp = await fetch("/api/fingerprints?" + params.toString());
    const data = await resp.json();
    allItems = data.items;
    render();
}

function render() {
    const list = document.getElementById("list");
    const q = document.getElementById("search").value.toLowerCase();
    const filtered = allItems.filter(it => {
        if (!q) return true;
        return (it.exc_type + " " + it.rel_path + " " + it.func_name).toLowerCase().includes(q);
    });
    document.getElementById("count").textContent =
        filtered.length + " of " + allItems.length;
    if (filtered.length === 0) {
        list.innerHTML = '<div class="empty">nothing matches</div>';
        return;
    }
    list.innerHTML = filtered.map(it => {
        const total = it.total_count || 0;
        const countTitle = total === 1 ? "1 occurrence" : `${total} occurrences`;
        return `
        <div class="entry ${it.resolved ? "resolved" : ""} ${expanded.has(it.fp) ? "open" : ""}" data-fp="${it.fp}">
            <div class="entry-head">
                ${total > 0 ? `<span class="count-tag" title="${countTitle}">${total}</span>` : ""}
                <span class="exc">${escapeHtml(it.exc_type)}</span>
                <span class="loc">${escapeHtml(it.rel_path)}:${escapeHtml(it.func_name)}()</span>
                <div class="spacer"></div>
                <span class="when" title="${fmtTs(it.last_activity)}">${fmtRel(it.last_activity)}</span>
                <button class="btn toggle">${it.resolved ? "Unresolve" : "Resolve"}</button>
            </div>
            <div class="details">
                <div><span class="k">first seen</span><span title="${fmtTs(it.first_seen)}">${fmtRel(it.first_seen)}</span></div>
                <div><span class="k">last notified</span><span title="${fmtTs(it.last_notified)}">${fmtRel(it.last_notified)}</span></div>
                <div><span class="k">last activity</span><span title="${fmtTs(it.last_activity)}">${fmtRel(it.last_activity)}</span></div>
                <div><span class="k">occurrences</span>${total}</div>
                ${it.last_message ? `<div class="section-label">last rendered payload</div><pre>${escapeHtml(it.last_message)}</pre>` : ""}
                <div class="fp-hash"><span class="fp-label">fingerprint:</span>${it.fp}</div>
            </div>
        </div>
    `;
    }).join("");
}

function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"]/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"
    })[c]);
}

document.querySelectorAll(".filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentFilter = btn.dataset.filter;
        fetchList();
    });
});

document.getElementById("search").addEventListener("input", render);

document.getElementById("list").addEventListener("click", async (e) => {
    const entry = e.target.closest(".entry");
    if (!entry) return;
    const fp = entry.dataset.fp;
    if (e.target.classList.contains("toggle")) {
        e.stopPropagation();
        const resolved = !entry.classList.contains("resolved");
        await fetch("/api/fingerprints/" + fp + "/resolved", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({resolved}),
        });
        fetchList();
        return;
    }
    // Only the header toggles expansion. Clicks inside .details must not
    // collapse the row — users need to select/copy the traceback text.
    if (!e.target.closest(".entry-head")) return;
    if (entry.classList.toggle("open")) {
        expanded.add(fp);
    } else {
        expanded.delete(fp);
    }
});

fetchList();
