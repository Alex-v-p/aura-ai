import nx from '@nx/eslint-plugin';

export default [
  ...nx.configs['flat/base'],
  ...nx.configs['flat/typescript'],
  { ignores: ['dist/**', 'node_modules/**', 'storybook-static/**'] },
  {
    files: ['**/*.ts'],
    rules: {
      '@nx/enforce-module-boundaries': [
        'error',
        {
          allow: [],
          enforceBuildableLibDependency: true,
          banTransitiveDependencies: true,
          depConstraints: [
            { sourceTag: 'scope:aura-web', onlyDependOnLibsWithTags: ['scope:aura', 'scope:shared'] },
            { sourceTag: 'scope:storybook', onlyDependOnLibsWithTags: ['scope:aura', 'scope:shared'] },
            { sourceTag: 'scope:aura', onlyDependOnLibsWithTags: ['scope:aura', 'scope:shared'] },
            { sourceTag: 'scope:shared', onlyDependOnLibsWithTags: ['scope:shared'] },
          ],
        },
      ],
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/explicit-function-return-type': 'off',
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
];
