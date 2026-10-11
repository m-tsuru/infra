# k3s-setup

Ansible で 3 台の k3s server を構築し、Flux に Kubernetes リソースの継続管理を引き渡します。
外部 Ansible Galaxy role は使用せず、このサービス内の `roles/k3s` と
`roles/gitops_bootstrap` を使用します。実機へのインストールはまだ実行していません。

## 構成と管理境界

| ホスト | IP | 役割 |
| --- | --- | --- |
| `sakuta.local.sasakulab.com` | `192.168.37.110` | server / etcd、初期化・GitOps bootstrap |
| `kaede.local.sasakulab.com` | `192.168.37.112` | server / etcd |
| `nodoka.local.sasakulab.com` | `192.168.37.113` | server / etcd |

3 台とも control-plane と worker を兼ねる embedded etcd の HA 構成です。
初回は sakuta → kaede → nodoka の順で参加させ、以降も `serial: 1` で変更します。
etcd の quorum は 2 台です。同時に 2 台を停止しないでください。

- **Ansible**: ホスト名、`/etc/hosts`、swap、カーネル設定、k3s バイナリ・設定・systemd、Flux 初回導入。
- **Flux**: Flux 自身、Sealed Secrets、`gitops/apps` に追加したリソースの継続的な同期・削除。
- **k3s 標準コンポーネント**: CoreDNS、metrics-server、local-path provisioner は k3s が管理します。
  これらのバージョンは Git 上の k3s バージョンに連動し、Flux と二重管理しません。
- **無効化**: k3s 同梱の Traefik と ServiceLB。Ingress / LoadBalancer は必要になった時点で GitOps に追加してください。
- **非 Kubernetes の機密情報**: Ansible Vault。k3s の参加トークン、必要なら sudo パスワード。
- **Kubernetes の機密情報**: kubeseal が生成する `SealedSecret` のみを Git に保存。

「すべて」は宣言的な設定をこのリポジトリで管理する意味です。OS / k3s の適用は Ansible を実行し、
Kubernetes の追加リソースは Flux が自動適用します。PV の実データ、etcd snapshot、復号鍵などの
バックアップは GitOps だけでは保護できません。

固定バージョンは k3s **`v1.37.1+k3s1`**、Flux **`v2.9.6`**、Sealed Secrets **`v0.40.0`** です。
k3s は公式 checksum ファイルで検証して取得します。Flux / Sealed Secrets の公式マニフェストは
SHA256 検証済みのものを vendor 化しています。

## ファイル配置

```text
ansible/
├── playbooks/
│   └── k3s-setup.yml
└── svc/
    └── k3s-setup/
        ├── inventory/
        │   ├── hosts.yml
        │   └── group_vars/k3s_cluster/
        │       ├── main.yml
        │       ├── vault.yml.example
        │       └── vault.yml              # 自分で作成する暗号化ファイル
        ├── roles/
        │   ├── k3s/
        │   └── gitops_bootstrap/
        ├── gitops/
        │   ├── clusters/local/flux-system/
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
- server は最低 2 CPU / 2 GiB RAM。Flux とアプリケーション分の余裕が必要です。etcd 用に SSD を推奨します。
- 3 台の IP は固定。Pod CIDR `10.42.0.0/16`、Service CIDR `10.43.0.0/16` と LAN / VPN の範囲が重複しないこと。
- GitHub、コンテナレジストリ、OS パッケージミラーへの外向き接続と、その名前を解決する既存 upstream resolver。
- 操作端末: Ansible Core 2.19–2.21。構築後の操作には `kubectl`、`kubeseal` v0.40.0、任意で Flux CLI。
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

## セットアップ

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

`vault.yml.example` は空の値を持つ説明用ファイルで、自動ロードされません。
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

`gitops/clusters/local/flux-system/gotk-sync.yaml` の参照先は次のとおりです。

- URL: `https://github.com/m-tsuru/infra`
- branch: `main`
- path: `./ansible/svc/k3s-setup/gitops/clusters/local`

別リポジトリを使用する場合はこのファイルを変更します。
**今回の bootstrap は公開リポジトリの HTTPS 読み取りを前提**とし、private repository の
認証 bootstrap は実装していません。Git の書き込み用 token / deploy key も不要です。

初回実行前に、この構成を参照先の branch に commit / push してください。
Flux は操作端末の作業ツリーではなく GitHub 上の内容を読みます。

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
kubectl -n flux-system get gitrepositories,kustomizations
kubectl -n kube-system rollout status deployment/sealed-secrets-controller
```

## 運用

- Kubernetes リソースは `gitops/apps` とその `kustomization.yaml` に追加して push します。
- Secret の暗号化、GitOps の構成、バージョン更新は [gitops/OPERATIONS.md](gitops/OPERATIONS.md) を参照してください。
- 全 Flux Kustomization は `prune: true`。Git から削除したリソースはクラスタからも削除されます。
  Namespace / PVC の削除や local-path volume のデータ損失に注意してください。
- 初回導入済みの Flux は通常の Ansible 再実行では再適用しません。初期化をやり直すための
  `-e gitops_bootstrap_force=true` は復旧時のみ使用し、普段のアップグレードには使用しません。
- k3s の再起動が必要な場合、既存の 3 台構成では他の server の `/readyz` を SSH 経由で確認してから
  1 台ずつ再起動します。他の server が不健全なら再起動前に停止します。初回の 1 台構成は例外です。
  2 台まで参加した途中の状態では、既存メンバーを再起動せず、未参加の 3 台目を `--limit` で先に追加してください。
- 適用済み revision は server が Ready になってから記録します。途中でダウンロード等が失敗しても、
  次回の実行で未反映の設定を検出して適用します。
- k3s の設定変更・バージョン更新は `roles/k3s/defaults/main.yml` または inventory の変数を変更し、
  `--tags k3s --ask-vault-pass` を付けて実行します。対応する Kubernetes の minor version を飛ばす更新は
  避け、アップグレード手順・snapshot・バックアップを事前確認してください。
- kubeconfig だけ再取得する場合は `--tags kubeconfig`、Flux の bootstrap だけなら `--tags gitops` を使用します。
  暗号化済み inventory がある場合は、どちらも `--ask-vault-pass` が必要です。

etcd snapshot は 6 時間ごと、各 server に 10 世代保存します。これはノード外バックアップではありません。
`/var/lib/rancher/k3s/server/db/snapshots` の snapshot と
`/var/lib/rancher/k3s/server/token` を、安全な暗号化済みの外部保管先へバックアップしてください。
Sealed Secrets の秘密鍵（`kube-system` 内のラベル
`sealed-secrets.bitnami.com/sealed-secrets-key` を持つ Secrets）も別途、安全に外部へ保存します。
鍵をその鍵自身で kubeseal する方法では復旧できません。クラスタを再作成する場合は、
Sealed Secrets を復元してから既存の暗号化リソースを使用してください。
local-path のデータはノードローカルなので、3 台の etcd HA とは別にアプリケーションのバックアップが必要です。

## ローカル検証の範囲

`tests` は inventory、ロールの機密情報保護、k3s 設定テンプレート、CA-bound token の選択、
途中失敗後の再適用、GitOps の依存関係・固定 manifest の checksum、平文 Secret / Vault の混入を検証します。
Ansible をインストールした Python 環境で実行してください（PyYAML が必要です）。実機への SSH / Kubernetes API 操作は行いません。
暗号化済み Vault の中身の妥当性、OS の実状態、CNI / etcd / Flux の稼働は構築後の確認が必要です。
