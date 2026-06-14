import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { FileText } from 'lucide-react';

interface Props {
  body: string;
  auxiliaryFiles: Record<string, string>;
}

const ReferenceSkillView: React.FC<Props> = ({ body, auxiliaryFiles }) => {
  const auxKeys = Object.keys(auxiliaryFiles);
  const [activeFile, setActiveFile] = useState<string | null>(null);

  const content = activeFile ? auxiliaryFiles[activeFile] : body;

  return (
    <div className="flex gap-6 h-full">
      {/* Auxiliary files sidebar */}
      {auxKeys.length > 0 && (
        <nav className="w-48 flex-shrink-0 border-r pr-4 overflow-y-auto"
             style={{ borderColor: 'var(--sidebar-border)' }}>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-1.5">
            <FileText size={13} />
            辅助文档
          </h4>
          <div className="space-y-1">
            {auxKeys.map(fname => (
              <button
                key={fname}
                onClick={() => setActiveFile(
                  activeFile === fname ? null : fname
                )}
                className={`w-full text-left px-2.5 py-1.5 rounded text-xs transition-colors ${
                  activeFile === fname
                    ? 'bg-teal-50 text-teal-700 font-medium'
                    : 'text-slate-500 hover:bg-slate-50'
                }`}
              >
                {fname}
              </button>
            ))}
          </div>
        </nav>
      )}

      {/* Markdown content */}
      <div className="flex-1 overflow-y-auto prose prose-sm max-w-none
                      prose-headings:text-slate-800 prose-headings:font-semibold
                      prose-p:text-slate-600 prose-code:text-teal-600
                      prose-code:bg-slate-50 prose-code:px-1 prose-code:py-0.5 prose-code:rounded
                      prose-pre:bg-slate-900 prose-pre:text-green-200
                      prose-a:text-teal-600 prose-strong:text-slate-700">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
    </div>
  );
};

export default ReferenceSkillView;
