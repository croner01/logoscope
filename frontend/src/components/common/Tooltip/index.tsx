import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle } from 'lucide-react';

type TooltipPlacement = 'top' | 'bottom';

const TOOLTIP_VIEWPORT_GUTTER = 14;
const TOOLTIP_GAP = 10;

interface TooltipProps {
  title: string;
  lines: string[];
  widthClass?: string;
  ariaLabel?: string;
}

const Tooltip: React.FC<TooltipProps> = ({ title, lines, widthClass = 'w-[320px]', ariaLabel }) => {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [layout, setLayout] = useState<{ left: number; top: number; width: number; placement: TooltipPlacement }>({
    left: 0,
    top: 0,
    width: 320,
    placement: 'bottom',
  });

  const tooltipWidth = useMemo(() => {
    const matched = /w-\[(\d+)px\]/.exec(widthClass);
    if (!matched) {
      return 320;
    }
    const parsed = Number(matched[1]);
    return Number.isFinite(parsed) ? parsed : 320;
  }, [widthClass]);

  const updatePosition = useCallback(() => {
    if (!triggerRef.current || typeof window === 'undefined') {
      return;
    }
    const rect = triggerRef.current.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    const resolvedWidth = Math.min(tooltipWidth, Math.max(240, viewportWidth - TOOLTIP_VIEWPORT_GUTTER * 2));
    const minLeft = TOOLTIP_VIEWPORT_GUTTER + resolvedWidth / 2;
    const maxLeft = viewportWidth - TOOLTIP_VIEWPORT_GUTTER - resolvedWidth / 2;
    const centeredLeft = rect.left + rect.width / 2;
    const left = Math.max(minLeft, Math.min(maxLeft, centeredLeft));
    const estimatedHeight = Math.max(124, 56 + lines.length * 24);
    const canPlaceBottom = rect.bottom + TOOLTIP_GAP + estimatedHeight <= viewportHeight - TOOLTIP_VIEWPORT_GUTTER;
    const placement: TooltipPlacement = canPlaceBottom ? 'bottom' : 'top';
    const top = canPlaceBottom
      ? rect.bottom + TOOLTIP_GAP
      : Math.max(TOOLTIP_VIEWPORT_GUTTER, rect.top - TOOLTIP_GAP - estimatedHeight);

    setLayout({ left, top, width: resolvedWidth, placement });
  }, [lines.length, tooltipWidth]);

  const showTooltip = useCallback(() => {
    updatePosition();
    setVisible(true);
  }, [updatePosition]);

  const hideTooltip = useCallback(() => {
    setVisible(false);
  }, []);

  useEffect(() => {
    if (!visible) {
      return undefined;
    }
    updatePosition();
    const handleViewportChange = () => updatePosition();
    window.addEventListener('resize', handleViewportChange);
    window.addEventListener('scroll', handleViewportChange, true);
    return () => {
      window.removeEventListener('resize', handleViewportChange);
      window.removeEventListener('scroll', handleViewportChange, true);
    };
  }, [visible, updatePosition]);

  return (
    <span className="relative inline-flex items-center" onMouseEnter={showTooltip} onMouseLeave={hideTooltip}>
      <button
        ref={triggerRef}
        type="button"
        className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-cyan-500/50 bg-slate-900/90 text-cyan-200 transition-colors hover:border-cyan-300 hover:text-cyan-100"
        aria-label={ariaLabel || `${title}说明`}
        onFocus={showTooltip}
        onBlur={hideTooltip}
        onClick={(event) => {
          event.preventDefault();
          setVisible((prev) => !prev);
        }}
      >
        <AlertCircle className="h-3.5 w-3.5" />
      </button>
      {visible && typeof document !== 'undefined'
        ? createPortal(
            <div
              className="pointer-events-none fixed z-[1300] rounded-xl border border-cyan-500/60 bg-slate-900/95 p-3 text-xs text-slate-100 shadow-[0_18px_40px_rgba(2,6,23,0.7)]"
              style={{ left: layout.left, top: layout.top, width: layout.width, transform: 'translateX(-50%)' }}
            >
              {layout.placement === 'bottom' ? (
                <div className="absolute -top-1 left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 border-l border-t border-cyan-500/60 bg-slate-900" />
              ) : (
                <div className="absolute -bottom-1 left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 border-r border-b border-cyan-500/60 bg-slate-900" />
              )}
              <div className="relative">
                <div className="mb-1.5 text-[11px] font-semibold tracking-wide text-cyan-200">{title}</div>
                <div className="space-y-1 text-[11px] leading-5 text-slate-200">
                  {lines.map((line) => (
                    <div key={`${title}-${line}`} className="flex items-start gap-1.5">
                      <span className="mt-1 h-1 w-1 rounded-full bg-cyan-300/80" />
                      <span>{line}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </span>
  );
};

export default Tooltip;
