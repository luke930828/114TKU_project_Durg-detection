import { ArrowLeft } from "lucide-react";
import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";
import * as XLSX from "xlsx";
import { saveAs } from "file-saver";

interface Props {
  onBack: () => void;
  type: "pdf" | "excel" | null;
}

export function ReportPreview({ onBack, type }: Props) {
  console.log("type =", type); // ⭐ 檢查用（可以之後刪掉）

  const data = [
    { name: "毒品交易網站", risk: "高風險", score: 95 },
    { name: "可疑網址", risk: "中風險", score: 68 },
    { name: "一般內容", risk: "低風險", score: 30 },
  ];

  // 📄 PDF下載
  const downloadPDF = () => {
    const doc = new jsPDF();

    doc.text("毒品交易風險分析報表", 14, 15);

    autoTable(doc, {
      startY: 20,
      head: [["案件名稱", "風險等級", "風險分數"]],
      body: data.map((item) => [item.name, item.risk, item.score]),
    });

    doc.save("report.pdf");
  };

  // 📊 Excel下載
  const downloadExcel = () => {
    const worksheet = XLSX.utils.json_to_sheet(data);
    const workbook = XLSX.utils.book_new();

    XLSX.utils.book_append_sheet(workbook, worksheet, "報表");

    const excelBuffer = XLSX.write(workbook, {
      bookType: "xlsx",
      type: "array",
    });

    const file = new Blob([excelBuffer], {
      type: "application/octet-stream",
    });

    saveAs(file, "report.xlsx");
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#2B4C7E] to-[#1a2f4f] p-6">
      <div className="max-w-5xl mx-auto bg-white rounded-2xl shadow-xl p-8">

        {/* 返回 */}
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-500 hover:text-blue-500 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          返回設定
        </button>

        {/* 標題 */}
        <h1 className="text-2xl font-bold mb-6 text-gray-800">
          報表預覽
        </h1>

        {/* KPI */}
        <div className="grid grid-cols-4 gap-4 mb-6">
          {[
            { label: "總案件", value: 128 },
            { label: "高風險", value: 32 },
            { label: "中風險", value: 45 },
            { label: "低風險", value: 51 },
          ].map((item, i) => (
            <div key={i} className="bg-blue-50 p-4 rounded-lg text-center">
              <div className="text-xl font-bold">{item.value}</div>
              <div className="text-sm text-gray-500">{item.label}</div>
            </div>
          ))}
        </div>

        {/* 表格 */}
        <div className="border rounded-lg overflow-hidden mb-6">
          <table className="w-full text-sm">
            <thead className="bg-gray-100">
              <tr>
                <th className="p-3 text-left">案件</th>
                <th className="text-center">風險</th>
                <th className="text-center">分數</th>
              </tr>
            </thead>
            <tbody>
              {data.map((item, i) => (
                <tr key={i} className="border-t">
                  <td className="p-3">{item.name}</td>
                  <td className="text-center">{item.risk}</td>
                  <td className="text-center">{item.score}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* ⭐ 單一下載按鈕（重點） */}
        <div className="flex justify-start">
          {type === "pdf" && (
            <button
              onClick={downloadPDF}
              className="bg-red-500 text-white px-6 py-2 rounded-lg hover:bg-red-600"
            >
              下載 PDF
            </button>
          )}

          {type === "excel" && (
            <button
              onClick={downloadExcel}
              className="bg-green-500 text-white px-6 py-2 rounded-lg hover:bg-green-600"
            >
              下載 Excel
            </button>
          )}
        </div>

      </div>
    </div>
  );
}