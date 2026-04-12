import type {
  RuntimeTranscriptBlock,
  RuntimeTranscriptMessage,
  RuntimeTranscriptPresentation,
} from '../types/view.js';

const isPreCommandPlanThinkingBlock = (block: RuntimeTranscriptBlock): boolean => (
  block.type === 'thinking'
  && String(block.title || '').trim() === '执行前计划'
);

const isDiagnosisStatusBlock = (block: RuntimeTranscriptBlock): boolean => (
  block.type === 'status'
  && String(block.id || '').startsWith('diagnosis-')
);

const isRecommendedCommandBlock = (block: RuntimeTranscriptBlock): boolean => (
  block.type === 'command'
  && String(block.id || '').startsWith('recommended-command-')
);

const isConversationBlock = (block: RuntimeTranscriptBlock): boolean => (
  block.type === 'answer'
  || block.type === 'approval'
  || block.type === 'user_input'
  || block.type === 'template_hint'
  || block.type === 'skill_step'
  || block.type === 'skill_matched'
  || isDiagnosisStatusBlock(block)
  || isRecommendedCommandBlock(block)
  || isPreCommandPlanThinkingBlock(block)
);

const isDetailBlock = (block: RuntimeTranscriptBlock): boolean => !isConversationBlock(block);

export const buildRuntimePresentation = (message: RuntimeTranscriptMessage): RuntimeTranscriptPresentation => {
  const conversationBlocks = message.blocks.filter(isConversationBlock);
  const detailBlocks = message.blocks.filter(isDetailBlock);
  return {
    conversation: {
      ...message,
      blocks: conversationBlocks,
    },
    detailBlocks,
    hasDetails: detailBlocks.length > 0,
  };
};

export default buildRuntimePresentation;
