/**
 * Local type declaration for ink-text-input — overrides the package's own types
 * to maintain a stable interface compatible with the ChatInput component.
 */
declare module 'ink-text-input' {
  import type React from 'react';

  export interface TextInputProps {
    value: string;
    onChange: (value: string) => void;
    onSubmit?: (value: string) => void;
    placeholder?: string;
    focus?: boolean;
    mask?: string;
    highlightPastedText?: boolean;
    showCursor?: boolean;
  }

  const TextInput: React.FC<TextInputProps>;
  export default TextInput;
}
