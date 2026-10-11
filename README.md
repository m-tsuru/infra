# infra

Sasakulab のインフラ構成を Git で管理します。

- [k3s / Argo CD のセットアップ](ansible/svc/k3s-setup/README.md)
- [構築済み Flux クラスタから Argo CD への移行](ansible/svc/k3s-setup/gitops/MIGRATE-TO-ARGOCD.md)
- [Argo CD / Sealed Secrets の運用](ansible/svc/k3s-setup/gitops/ARGOCD-OPERATIONS.md)

既存クラスタの移行には `ansible/playbooks/k3s-argocd.yml` を使用します。
変更を push しただけでは移行しません。旧 Flux マニフェストは誤削除防止と切り戻しのため残しています。
