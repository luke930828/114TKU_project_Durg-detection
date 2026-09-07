interface Props {
  url: string;
  siteType: 'social' | 'darkweb' | 'ecommerce' | 'messenger';
  onClose: () => void;
}

export function MockSitePreview({ url, siteType, onClose }: Props) {
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white w-[90%] h-[80%] rounded-xl overflow-hidden shadow-xl">
        
        {/* Header */}
        <div className="flex justify-between items-center px-4 py-2 bg-[#2B4C7E] text-white">
          <span>{url}</span>
          <button onClick={onClose}>✕</button>
        </div>

        {/* Content */}
        <div className="p-6 text-center">
          <h2 className="text-2xl mb-4">模擬網站預覽</h2>
          <p className="text-gray-600 mb-2">類型：{siteType}</p>
          <p className="text-blue-500 break-all">{url}</p>
        </div>

      </div>
    </div>
  );
}