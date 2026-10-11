# k3s-setup

Ansible で 3 台の k3s server を構築し、Argo CD に Kubernetes リソースの継続管理を引き渡します。
外部 Ansible Galaxy role は使用せず、このサービス内の `roles/k3s` と
`roles/argocd_bootstrap` を使用します。

**既に Flux でセットアップ済みの場合は再構築しないでください。**
[Flux → Argo CD の移行手順](gitops/MIGRATE-TO-ARGOCD.md) に従い、
`ansible/playbooks/k3s-argocd.yml` で GitOps 部分だけを切り替えます。
旧 Flux の manifest / role は、稼働中リソースの誤削除防止と切り戻しのため残しています。
アシスタントは実機の移行・再インストールを実行していません。

## 構成と管理境界

| ホスト | IP | 役割 |
| --- | --- | --- |
| `sakuta.local.sasakulab.com` | `192.168.37.110` | server / etcd、初期化・GitOps bootstrap |
| `kaede.local.sasakulab.com` | `192.168.37.112` | server / etcd |
| `nodoka.local.sasakulab.com` | `192.168.37.113` | server / etcd |

3 台とも control-plane と worker を兼ねる embedded etcd の HA 構成です。
初回は sakuta → kaede → nodoka の順で参加させ、以降も `serial: 1` で変更します。
etcd の quorum は 2 台です。同時に 2 台を停止しないでください。

- **Ansible**: ホスト名、`/etc/hosts`、swap、カーネル設定、k3s バイナリ・設定・systemd、Argo CD 初回導入と root Application の初期設定。
- **Argo CD**: Argo CD 自身、Sealed Secrets、`gitops/apps` と子 Application の継続的な同期。
  移行時の誤削除防止のため自動 prune は無効です。削除の自動化は運用確認後に対象ごとに有効化します。
- **k3s 標準コンポーネント**: CoreDNS、metrics-server、local-path provisioner は k3s が管理します。
  これらのバージョンは Git 上の k3s バージョンに連動し、Argo CD と二重管理しません。
- **無効化**: k3s 同梱の Traefik と ServiceLB。Ingress / LoadBalancer は必要になった時点で GitOps に追加してください。
- **非 Kubernetes の機密情報**: Ansible Vault。k3s の参加トークン、必要なら sudo パスワード。
- **Kubernetes の機密情報**: kubeseal が生成する `SealedSecret` のみを Git に保存。

「すべて」は宣言的な設定をこのリポジトリで管理する意味です。OS / k3s の適用は Ansible を実行し、
Kubernetes の追加リソースは Argo CD が自動適用します。PV の実データ、etcd snapshot、復号鍵などの
バックアップは GitOps だけでは保護できません。

固定バージョンは k3s **`v1.37.1+k3s1`**、Argo CD **`v3.5.4`**、Sealed Secrets **`v0.40.0`** です。
k3s は公式 checksum ファイルで検証して取得します。Argo CD / Sealed Secrets の公式マニフェストは
SHA256 検証済みのものを vendor 化しています。旧 Flux **`v2.9.6`** は移行完了まで残します。
Argo CD は通常の非 HA インストールです。k3s の etcd HA とは別に容量・可用性を確認してください。

## ファイル配置

```text
ansible/
├── playbooks/
│   ├── k3s-setup.yml                    # 新規構築
│   └── k3s-argocd.yml                   # 既存クラスタの GitOps 部分のみ
└── svc/
    └── k3s-setup/
        ├── inventory/
        │   ├── hosts.yml
        │   └── group_vars/k3s_cluster/
        │       ├── main.yml
        │       └── vault.yml              # 自分で作成する暗号化ファイル
        ├── roles/
        │   ├── k3s/
        │   ├── argocd_bootstrap/
        │   └── gitops_bootstrap/           # 旧 Flux（移行用に保持）
        ├── gitops/
        │   ├── argocd/                    # Argo install / Applications
        │   ├── clusters/local/flux-system/ # 旧 Flux（稼働中は削除しない）
        │   ├── infrastructure/sealed-secrets/
        │   └── apps/
        ├── tests/
        └── artifacts/                     # 実行時生成、Git 管理外
```

リポジトリルートの `ansible.cfg` が inventory と role の場所を設定します。
以下のコマンドは**リポジトリルートから**実行してください。

## 前提条件

- 各ホスト: **Debian / Ubuntu、systemd、amd64 / arm64**、Python 3、SSH、sudo 権限。
  対応外 OS / アーキテクチャでは変更前の検証で停止します。
- server は最低 2 CPU / 2 GiB RAM。Argo CD とアプリケーション分の余裕が必要です。etcd 用に SSD を推奨します。
- 3 台の IP は固定。Pod CIDR `10.42.0.0/16`、Service CIDR `10.43.0.0/16` と LAN / VPN の範囲が重複しないこと。
- GitHub、コンテナレジストリ、OS パッケージミラーへの外向き接続と、その名前を解決する既存 upstream resolver。
- 操作端末: Ansible Core 2.19–2.21。構築後の操作には `kubectl`、`kubeseal` v0.40.0、任意で Argo CD CLI（旧 Flux 撤去時だけ Flux CLI）。
- メモリ cgroup が有効で、`overlay` / `br_netfilter` がロード可能であること。
- swap は無効化します。ロールは実行中の swap と `/etc/fstab` を処理しますが、zram / 独自 systemd unit の
  自動有効化は別途停止してください。再起動後も swap が戻らないことを確認してください。

ファイアウォールは勝手に無効化・変更しません。必要な通信を事前に許可してください。

| 通信元 → 通信先 | ポート | 用途 |
| --- | --- | --- |
| 操作端末 → 各ノード | TCP 22 | SSH |
| ノード・操作端末 → server | TCP 6443 | k3s / Kubernetes API |
| server → server | TCP 2379–2380 | embedded etcd |
| ノード → ノード | UDP 8472 | Flannel VXLAN |
| ノード → ノード | TCP 10250 | kubelet / metrics-server |

Pod / Service CIDR の転送・必要な outbound 通信も許可してください。
**etcd / VXLAN / kubelet をインターネットに公開しないでください。**

## DNS と API 接続先

Ansible の接続先は `ansible_host` の IP なので、ローカル DNS なしで構築できます。
各ノードには以下の `/etc/hosts` エントリを管理ブロックとして追加します。
必要なら操作端末にも手動で追加してください。

```text
192.168.37.110 sakuta.local.sasakulab.com sakuta
192.168.37.112 kaede.local.sasakulab.com kaede
192.168.37.113 nodoka.local.sasakulab.com nodoka
```

これは LAN 用 DNS サーバーの構築ではありません。端末全体に配布するなら後で内部 DNS に
これらの A レコードを登録してください。ノードの `/etc/hosts` が Pod に継承されるわけではありません。
Pod からの LAN 名解決は必要に応じて CoreDNS / 内部 DNS を別途設定します。
`sasakulab.com` の公開 DNS や証明書設定は変更しません。

初期参加と取得する kubeconfig の API endpoint は **`https://192.168.37.110:6443`** です。
証明書 SAN には 3 台の FQDN / IP を含めます。ただし **API の VIP / ロードバランサーは未構築**です。
sakuta 停止中は、kubeconfig の `server` を別の稼働中 server の IP に切り替えてください。
固定の HA endpoint が必要になったら、未割り当ての VIP / LB を決めて追加してください。

## 新規クラスタのセットアップ

以下はまだ構築していないクラスタ用です。既存クラスタの Vault / k3s / sealing key は作り直さず、
[移行手順](gitops/MIGRATE-TO-ARGOCD.md) を使用してください。

### 1. Ansible と SSH の準備

既存の Python 仮想環境等にインストールします。

```sh
python3 -m pip install -r ansible/svc/k3s-setup/requirements.txt
ansible-inventory --graph
```

`inventory/group_vars/k3s_cluster/main.yml` に `ansible_user` を設定するか、実行時に
`--user` を渡してください。SSH 秘密鍵は通常の SSH agent / SSH 設定で指定できます。
ホスト鍵チェックは有効です。初回 SSH 接続で鍵の fingerprint を確認してください。

### 2. Ansible Vault の作成

```sh
openssl rand -hex 32
ansible-vault create ansible/svc/k3s-setup/inventory/group_vars/k3s_cluster/vault.yml
```

エディタに次を入力し、トークンには生成した値を設定します。

```yaml
vault_k3s_token: "生成したランダムトークン"
# パスワード付き sudo の場合のみ:
# ansible_become_password: "sudo のパスワード"
```

作成した `vault.yml` が `$ANSIBLE_VAULT;` で始まる暗号化ファイルであることを確認し、
**暗号化済みの `vault.yml` は Git に保存**してください。Vault のパスワードは Git 管理外です。
平文の `vault.yml` を作成・commit しないでください。

参加トークンは各ノードの root 専用ファイルにのみ配置し、ログやプロセスの引数には渡しません。
Vault のランダムな password は初期 server の作成に使い、後続 server には初期 server が生成した
CA hash 付きの完全な `K10…` token を、ホスト鍵検証付き SSH 経由で取得して渡します。
既存 server token と Vault の password が一致しない場合は、変更せずに停止します。
既存クラスタで Vault のトークンだけを変更しないでください。トークンのローテーションは
k3s の正式な手順で行い、etcd の復旧に必要な server token もバックアップしてください。

### 3. GitOps の参照先を確認し、先に push

`gitops/argocd/applications/*.yaml` の全 Application が次のリポジトリを参照します。

- URL: `https://github.com/m-tsuru/infra.git`
- branch: `main`
- root path: `ansible/svc/k3s-setup/gitops/argocd/applications`

別リポジトリを使用する場合は root と全 child Application の source を揃えて変更します。
**今回の bootstrap は公開リポジトリの HTTPS 読み取りを前提**とし、private repository の
認証 bootstrap は実装していません。Git の書き込み用 token / deploy key も不要です。

初回実行前に、この構成を参照先の branch に commit / push してください。
Argo CD は操作端末の作業ツリーではなく GitHub 上の内容を読みます。

### 4. 検証して実行

```sh
ansible-playbook ansible/playbooks/k3s-setup.yml --syntax-check --ask-vault-pass
python3 -m unittest discover -s ansible/svc/k3s-setup/tests -v
ansible-playbook ansible/playbooks/k3s-setup.yml --ask-vault-pass
```

sudo パスワードを Vault に保存しない場合は最後のコマンドに `--ask-become-pass` を追加します。
初回は 3 台すべてを対象にしてください。`--check` は実機の状態を確認するため、空のホストに
対する完全なインストールシミュレーションにはなりません。

kubeconfig は `ansible/svc/k3s-setup/artifacts/kubeconfig.yaml` に `0600`、親ディレクトリ `0700` で
保存します。**cluster-admin 資格情報なので、Git への追加や共有はしないでください。**

```sh
export KUBECONFIG=ansible/svc/k3s-setup/artifacts/kubeconfig.yaml
kubectl get nodes -o wide
kubectl -n argocd get applications.argoproj.io \
  -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
kubectl -n kube-system rollout status deployment/sealed-secrets-controller
```

## 運用

- Kubernetes リソースは `gitops/apps` とその `kustomization.yaml` に追加して push します。
- UI 接続、Secret の暗号化、GitOps の構成、バージョン更新は [gitops/ARGOCD-OPERATIONS.md](gitops/ARGOCD-OPERATIONS.md) を参照してください。
- 全 Application は自動 sync / self-heal が有効ですが、`prune: false`。Git から削除したリソースは自動削除されません。
  移行・運用確認後に対象ごとに prune を有効化できます。Namespace / PVC のデータ損失には注意してください。
- root 導入済みの Argo は通常の Ansible 再実行では再適用しません。root 自身は bootstrap オブジェクトとして
  Git に宣言され、循環待機を避けるため子の同期対象から除外します。root spec の変更時だけ専用 Playbook に
  `-e argocd_bootstrap_reapply_root=true` を付けます。既存 controller は再インストールしません。
- 旧 Flux が残っている場合は、停止と削除保護を確認し `-e argocd_migrate_from_flux=true` が必要です。
  自動停止・自動削除はしません。通常の Argo アップグレードは Git で行います。
- k3s の再起動が必要な場合、既存の 3 台構成では他の server の `/readyz` を SSH 経由で確認してから
  1 台ずつ再起動します。他の server が不健全なら再起動前に停止します。初回の 1 台構成は例外です。
  2 台まで参加した途中の状態では、既存メンバーを再起動せず、未参加の 3 台目を `--limit` で先に追加してください。
- 適用済み revision は server が Ready になってから記録します。途中でダウンロード等が失敗しても、
  次回の実行で未反映の設定を検出して適用します。
- k3s の設定変更・バージョン更新は `roles/k3s/defaults/main.yml` または inventory の変数を変更し、
  `--tags k3s --ask-vault-pass` を付けて実行します。対応する Kubernetes の minor version を飛ばす更新は
  避け、アップグレード手順・snapshot・バックアップを事前確認してください。
- kubeconfig だけ再取得する場合は `--tags kubeconfig`、Argo CD の bootstrap だけなら `--tags gitops` を使用します。
  暗号化済み inventory がある場合は、どちらも `--ask-vault-pass` が必要です。

etcd snapshot は 6 時間ごと、各 server に 10 世代保存します。これはノード外バックアップではありません。
`/var/lib/rancher/k3s/server/db/snapshots` の snapshot と
`/var/lib/rancher/k3s/server/token` を、安全な暗号化済みの外部保管先へバックアップしてください。
Sealed Secrets の秘密鍵（`kube-system` 内のラベル
`sealedsecrets.bitnami.com/sealed-secrets-key` を持つ Secrets）も別途、安全に外部へ保存します。
鍵をその鍵自身で kubeseal する方法では復旧できません。クラスタを再作成する場合は、
Sealed Secrets を復元してから既存の暗号化リソースを使用してください。
local-path のデータはノードローカルなので、3 台の etcd HA とは別にアプリケーションのバックアップが必要です。

## ローカル検証の範囲

`tests` は inventory、ロールの機密情報保護、k3s 設定テンプレート、CA-bound token の選択、
途中失敗後の再適用、GitOps の依存関係・固定 manifest の checksum、機密値を含む平文 Secret / Vault の混入を検証します。
Argo の公式 manifest にある空の `argocd-secret` / `argocd-notifications-secret` だけは許可し、
実際の credential data の混入は拒否します。
移行用の mock テストは opt-in、停止、削除保護、source 一致、root の再実行動作を検証します。
Ansible をインストールした Python 環境で実行してください（PyYAML が必要です）。実機への SSH / Kubernetes API 操作は行いません。
暗号化済み Vault の中身の妥当性、OS の実状態、CNI / etcd / Argo CD の稼働は構築後の確認が必要です。
