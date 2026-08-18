export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    // gitops: CI commits the built image SHA back into the manifests, matching
    // the platform repository's convention so history reads the same in both.
    'type-enum': [
      2,
      'always',
      ['feat', 'fix', 'docs', 'chore', 'ci', 'refactor', 'test', 'perf', 'build', 'style', 'revert', 'gitops'],
    ],
    // Dependabot bodies carry dependency metadata and release links that cannot
    // be wrapped. The header limit still applies.
    'body-max-line-length': [0, 'always'],
    'footer-max-line-length': [0, 'always'],
  },
};
