import { Brain, Moon, Monitor, Sun } from 'lucide-react';
import { Segmented } from '../../../components/ui/Toggle';
import { useUiStore, type ThemePref, type ThinkingPref } from '../../../stores/useUiStore';
import { Group, Row } from '../parts';

export function General() {
  const theme = useUiStore((s) => s.theme);
  const setTheme = useUiStore((s) => s.setTheme);
  const thinking = useUiStore((s) => s.thinking);
  const setThinking = useUiStore((s) => s.setThinking);
  return (
    <Group>
      <Row title="主题">
        <Segmented<ThemePref>
          label="主题"
          value={theme}
          onChange={setTheme}
          options={[
            { value: 'light', label: '浅色', icon: <Sun size={13} /> },
            { value: 'dark', label: '暗色', icon: <Moon size={13} /> },
            { value: 'system', label: '系统', icon: <Monitor size={13} /> },
          ]}
        />
      </Row>
      <Row
        title={
          <span className="flex items-center gap-1.5">
            <Brain size={14} />
            思考链
          </span>
        }
      >
        <Segmented<ThinkingPref>
          label="思考链"
          value={thinking}
          onChange={setThinking}
          options={[
            { value: 'expanded', label: '展开' },
            { value: 'collapsed', label: '折叠' },
            { value: 'hidden', label: '隐藏' },
          ]}
        />
      </Row>
    </Group>
  );
}
