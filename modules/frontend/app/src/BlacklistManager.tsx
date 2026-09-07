import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { hasMaliciousInput, MALICIOUS_INPUT_MESSAGE } from "./inputSecurity";

interface Props {
  onBack: () => void;
}

export default function BlacklistManager({ onBack }: Props) {
  const [list, setList] = useState<string[]>([
    "dark-market.com",
    "drug-sale.org",
  ]);

  const [input, setInput] = useState("");

  const addItem = () => {
    if (!input) return;
    if (hasMaliciousInput([input])) {
      alert(MALICIOUS_INPUT_MESSAGE);
      return;
    }
    setList([...list, input]);
    setInput("");
  };

  const removeItem = (index: number) => {
    const newList = [...list];
    newList.splice(index, 1);
    setList(newList);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#2B4C7E] to-[#1a2f4f] p-6">
      <div className="max-w-4xl mx-auto bg-white rounded-2xl p-8 shadow-xl">

        {/* 返回 */}
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-500 hover:text-blue-500 mb-6"
        >
          <ArrowLeft className="w-5 h-5" />
          返回
        </button>

        <h1 className="text-xl font-bold mb-6">黑名單管理</h1>

        {/* 新增 */}
        <div className="flex gap-3 mb-6">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="輸入網址..."
            className="flex-1 border px-4 py-2 rounded-lg"
          />
          <button
            onClick={addItem}
            className="bg-red-500 text-white px-4 rounded-lg"
          >
            新增
          </button>
        </div>

        {/* 列表 */}
        <div className="space-y-2">
          {list.map((item, i) => (
            <div
              key={i}
              className="flex justify-between items-center border p-3 rounded-lg"
            >
              <span>{item}</span>
              <button
                onClick={() => removeItem(i)}
                className="text-red-500"
              >
                移除
              </button>
            </div>
          ))}
        </div>

      </div>
    </div>
  );
}
