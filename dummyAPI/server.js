const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = Number(process.env.PORT) || 3000;
const PDF_DIR = path.join(__dirname, "PDF");

const tenderSeed = [
  {
    title: "Construction and Maintenance Tender",
    tenderNo: "DUMMY/TENDER/2026/001",
    organization: "Demo Public Works Department",
    department: "Civil Works",
    category: "Works",
    location: "New Delhi",
    tenderValue: 1250000,
    emdAmount: 25000,
    publishDate: "2026-04-28",
    bidSubmissionStartDate: "2026-04-29",
    bidSubmissionEndDate: "2026-05-12",
    bidOpeningDate: "2026-05-13",
    status: "Open"
  },
  {
    title: "Supply of Office Equipment",
    tenderNo: "DUMMY/TENDER/2026/002",
    organization: "Demo Municipal Corporation",
    department: "Procurement",
    category: "Goods",
    location: "Mumbai",
    tenderValue: 875000,
    emdAmount: 17500,
    publishDate: "2026-04-28",
    bidSubmissionStartDate: "2026-04-30",
    bidSubmissionEndDate: "2026-05-15",
    bidOpeningDate: "2026-05-16",
    status: "Open"
  },
  {
    title: "IT Support and AMC Services",
    tenderNo: "DUMMY/TENDER/2026/003",
    organization: "Demo Digital Services",
    department: "Information Technology",
    category: "Services",
    location: "Bengaluru",
    tenderValue: 1650000,
    emdAmount: 33000,
    publishDate: "2026-04-28",
    bidSubmissionStartDate: "2026-05-01",
    bidSubmissionEndDate: "2026-05-20",
    bidOpeningDate: "2026-05-21",
    status: "Open"
  }
];

function sendJson(res, statusCode, body) {
  const payload = JSON.stringify(body, null, 2);

  res.writeHead(statusCode, {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json; charset=utf-8"
  });
  res.end(payload);
}

function sendNotFound(res, message = "Route not found") {
  sendJson(res, 404, {
    success: false,
    message
  });
}

function getBaseUrl(req) {
  return `http://${req.headers.host}`;
}

function getPdfFiles() {
  if (!fs.existsSync(PDF_DIR)) {
    return [];
  }

  return fs
    .readdirSync(PDF_DIR, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".pdf"))
    .map((entry) => entry.name)
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

function createTenderFromPdf(fileName, index, baseUrl) {
  const seed = tenderSeed[index] || {
    title: `Dummy Tender ${index + 1}`,
    tenderNo: `DUMMY/TENDER/2026/${String(index + 1).padStart(3, "0")}`,
    organization: "Demo Tender Organization",
    department: "General",
    category: "Tender",
    location: "India",
    tenderValue: 500000,
    emdAmount: 10000,
    publishDate: "2026-04-28",
    bidSubmissionStartDate: "2026-04-29",
    bidSubmissionEndDate: "2026-05-15",
    bidOpeningDate: "2026-05-16",
    status: "Open"
  };

  const stats = fs.statSync(path.join(PDF_DIR, fileName));
  const id = index + 1;

  return {
    id,
    ...seed,
    currency: "INR",
    contact: {
      name: "Tender Helpdesk",
      email: "helpdesk@example.com",
      phone: "+91-9999999999"
    },
    documents: [
      {
        id: `DOC-${String(id).padStart(3, "0")}`,
        name: fileName,
        type: "pdf",
        sizeBytes: stats.size,
        viewUrl: `${baseUrl}/api/tenders/${id}/pdf`,
        downloadUrl: `${baseUrl}/api/tenders/${id}/pdf?download=true`,
        directUrl: `${baseUrl}/pdf/${encodeURIComponent(fileName)}`
      }
    ]
  };
}

function getTenders(baseUrl) {
  return getPdfFiles().map((fileName, index) => createTenderFromPdf(fileName, index, baseUrl));
}

function servePdf(req, res, fileName, asDownload = false) {
  const safeFileName = path.basename(fileName);
  const filePath = path.join(PDF_DIR, safeFileName);

  if (!safeFileName.toLowerCase().endsWith(".pdf") || !fs.existsSync(filePath)) {
    sendNotFound(res, "PDF not found");
    return;
  }

  const disposition = asDownload ? "attachment" : "inline";

  res.writeHead(200, {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/pdf",
    "Content-Disposition": `${disposition}; filename="${safeFileName}"`
  });

  fs.createReadStream(filePath).pipe(res);
}

function handleRequest(req, res) {
  const url = new URL(req.url, getBaseUrl(req));
  const pathname = decodeURIComponent(url.pathname);

  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type"
    });
    res.end();
    return;
  }

  if (req.method !== "GET") {
    sendJson(res, 405, {
      success: false,
      message: "Only GET method is supported"
    });
    return;
  }

  if (pathname === "/") {
    sendJson(res, 200, {
      success: true,
      message: "Dummy Tender API is running",
      endpoints: {
        tenders: "/api/tenders",
        tenderById: "/api/tenders/1",
        tenderPdf: "/api/tenders/1/pdf",
        directPdf: "/pdf/tender.pdf"
      }
    });
    return;
  }

  if (pathname === "/api/tenders") {
    const tenders = getTenders(getBaseUrl(req));
    sendJson(res, 200, {
      success: true,
      total: tenders.length,
      data: tenders
    });
    return;
  }

  const tenderMatch = pathname.match(/^\/api\/tenders\/(\d+)$/);
  if (tenderMatch) {
    const id = Number(tenderMatch[1]);
    const tender = getTenders(getBaseUrl(req)).find((item) => item.id === id);

    if (!tender) {
      sendNotFound(res, "Tender not found");
      return;
    }

    sendJson(res, 200, {
      success: true,
      data: tender
    });
    return;
  }

  const tenderPdfMatch = pathname.match(/^\/api\/tenders\/(\d+)\/pdf$/);
  if (tenderPdfMatch) {
    const id = Number(tenderPdfMatch[1]);
    const files = getPdfFiles();
    const fileName = files[id - 1];

    if (!fileName) {
      sendNotFound(res, "Tender PDF not found");
      return;
    }

    servePdf(req, res, fileName, url.searchParams.get("download") === "true");
    return;
  }

  const directPdfMatch = pathname.match(/^\/pdf\/(.+\.pdf)$/i);
  if (directPdfMatch) {
    servePdf(req, res, directPdfMatch[1], url.searchParams.get("download") === "true");
    return;
  }

  sendNotFound(res);
}

const server = http.createServer(handleRequest);

server.listen(PORT, () => {
  console.log(`Dummy Tender API running at http://localhost:${PORT}`);
  console.log(`Tender list: http://localhost:${PORT}/api/tenders`);
});
