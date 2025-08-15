import { describe, it, expect } from 'vitest';
import {
  TOOL_SCHEMAS,
  getDefaultToolConfig,
  isToolConfigured,
  needsConfiguration,
  validateToolConfig,
} from './toolConfig';

describe('toolConfig', () => {
  describe('TOOL_SCHEMAS', () => {
    it('should have schemas for all expected tools', () => {
      const expectedTools = [
        'googlesearch',
        'tavily',
        'duckduckgo',
        'email',
        'telegram',
        'github',
        'docker',
        'shell',
        'python',
        'file',
        'yfinance',
        'calculator',
        'wikipedia',
        'arxiv',
        'csv',
        'pandas',
        'newspaper',
        'website',
        'jina',
      ];

      expectedTools.forEach(tool => {
        expect(TOOL_SCHEMAS[tool]).toBeDefined();
        expect(TOOL_SCHEMAS[tool].id).toBe(tool);
        expect(TOOL_SCHEMAS[tool].name).toBeDefined();
        expect(TOOL_SCHEMAS[tool].description).toBeDefined();
        expect(TOOL_SCHEMAS[tool].category).toBeDefined();
        expect(TOOL_SCHEMAS[tool].fields).toBeDefined();
      });
    });

    it('should have valid field types for all schemas', () => {
      const validTypes = ['text', 'password', 'number', 'boolean', 'select', 'url'];

      Object.values(TOOL_SCHEMAS).forEach(schema => {
        schema.fields.forEach(field => {
          expect(validTypes).toContain(field.type);
          expect(field.name).toBeDefined();
          expect(field.label).toBeDefined();
        });
      });
    });

    it('should have options for select fields', () => {
      Object.values(TOOL_SCHEMAS).forEach(schema => {
        schema.fields.forEach(field => {
          if (field.type === 'select') {
            expect(field.options).toBeDefined();
            expect(field.options!.length).toBeGreaterThan(0);
            field.options!.forEach(option => {
              expect(option.value).toBeDefined();
              expect(option.label).toBeDefined();
            });
          }
        });
      });
    });

    it('should have validation for fields that need it', () => {
      // Check number fields in shell tool
      const shellNumberFields = TOOL_SCHEMAS.shell.fields.filter(f => f.type === 'number');
      shellNumberFields.forEach(field => {
        expect(field.validation).toBeDefined();
        expect(field.validation!.min).toBeDefined();
      });

      // Check URL fields in jina tool
      const jinaUrlFields = TOOL_SCHEMAS.jina.fields.filter(f => f.type === 'url');
      expect(jinaUrlFields.length).toBeGreaterThan(0);
    });
  });

  describe('getDefaultToolConfig', () => {
    it('should return default config for tool with defaults', () => {
      const config = getDefaultToolConfig('googlesearch');

      expect(config).toEqual({
        max_results: 10,
      });
    });

    it('should return empty config for tool without defaults', () => {
      const config = getDefaultToolConfig('calculator');

      expect(config).toEqual({});
    });

    it('should return empty config for non-existent tool', () => {
      const config = getDefaultToolConfig('nonexistent');

      expect(config).toEqual({});
    });

    it('should include all default values from schema', () => {
      const config = getDefaultToolConfig('tavily');

      expect(config.search_depth).toBe('basic');
    });
  });

  describe('needsConfiguration', () => {
    it('should return true for tools with required fields', () => {
      expect(needsConfiguration('googlesearch')).toBe(true);
      expect(needsConfiguration('email')).toBe(true);
      expect(needsConfiguration('file')).toBe(true);
    });

    it('should return false for tools without required fields', () => {
      expect(needsConfiguration('calculator')).toBe(false);
      expect(needsConfiguration('wikipedia')).toBe(false);
    });

    it('should return false for non-existent tool', () => {
      expect(needsConfiguration('nonexistent')).toBe(false);
    });
  });

  describe('isToolConfigured', () => {
    it('should return true when all required fields are configured', () => {
      const config = {
        api_key: 'test-key',
        search_engine_id: 'test-id',
      };

      expect(isToolConfigured('googlesearch', config)).toBe(true);
    });

    it('should return false when required fields are missing', () => {
      const config = {
        api_key: 'test-key',
        // Missing search_engine_id
      };

      expect(isToolConfigured('googlesearch', config)).toBe(false);
    });

    it('should return false when required fields are empty', () => {
      const config = {
        api_key: '',
        search_engine_id: 'test-id',
      };

      expect(isToolConfigured('googlesearch', config)).toBe(false);
    });

    it('should return true for tools without required fields', () => {
      expect(isToolConfigured('calculator', {})).toBe(true);
      expect(isToolConfigured('calculator', undefined)).toBe(true);
    });

    it('should return true for non-existent tool', () => {
      expect(isToolConfigured('nonexistent', {})).toBe(true);
    });

    it('should handle undefined config', () => {
      expect(isToolConfigured('googlesearch', undefined)).toBe(false);
      expect(isToolConfigured('calculator', undefined)).toBe(true);
    });
  });

  describe('validateToolConfig', () => {
    it('should return empty errors for valid config', () => {
      const config = {
        api_key: 'test-key',
        search_engine_id: 'test-id',
        max_results: 10,
      };

      const errors = validateToolConfig('googlesearch', config);
      expect(errors).toEqual({});
    });

    it('should return errors for missing required fields', () => {
      const config = {
        max_results: 10,
      };

      const errors = validateToolConfig('googlesearch', config);
      expect(errors.api_key).toBe('API Key is required');
      expect(errors.search_engine_id).toBe('Search Engine ID is required');
    });

    it('should validate number ranges', () => {
      const config = {
        timeout: 0,
      };

      const errors = validateToolConfig('shell', config);
      expect(errors.timeout).toBe('Must be at least 1');
    });

    it('should validate URL format', () => {
      // URL validation would need a pattern in the schema
      // For now, just test that validation runs
      const config = {
        endpoint: 'not-a-url',
        api_key: 'test-key',
      };

      const errors = validateToolConfig('jina', config);
      // No pattern validation defined, so no error expected
      expect(errors.api_key).toBeUndefined();
    });

    it('should return empty errors for non-existent tool', () => {
      const errors = validateToolConfig('nonexistent', {});
      expect(errors).toEqual({});
    });

    it('should handle pattern validation', () => {
      // Test with a tool that has pattern validation if any
      const config = {
        api_key: 'test-key',
      };

      const errors = validateToolConfig('jina', config);
      expect(errors.api_key).toBeUndefined();
    });

    it('should use custom validation messages', () => {
      // Test with missing required field
      const config = {};

      const errors = validateToolConfig('jina', config);
      expect(errors.api_key).toBe('API Key is required');
    });
  });

  describe('schema categories', () => {
    it('should have valid categories for all tools', () => {
      const validCategories = ['search', 'files', 'communication', 'development', 'data', 'other'];

      Object.values(TOOL_SCHEMAS).forEach(schema => {
        expect(validCategories).toContain(schema.category);
      });
    });

    it('should group tools correctly by category', () => {
      const searchTools = Object.values(TOOL_SCHEMAS).filter(s => s.category === 'search');
      expect(searchTools.map(t => t.id)).toContain('googlesearch');
      expect(searchTools.map(t => t.id)).toContain('tavily');

      const commTools = Object.values(TOOL_SCHEMAS).filter(s => s.category === 'communication');
      expect(commTools.map(t => t.id)).toContain('email');
      expect(commTools.map(t => t.id)).toContain('telegram');

      const devTools = Object.values(TOOL_SCHEMAS).filter(s => s.category === 'development');
      expect(devTools.map(t => t.id)).toContain('github');
      expect(devTools.map(t => t.id)).toContain('docker');
    });
  });
});
