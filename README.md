# Awoke (地震情報 Discord 通知サービス)

## AWOKE
『ラブライブ!蓮ノ空女学院スクールアイドルクラブ』から派生したミニユニットDOLLCHESTRAのデビュー曲。([ピクシブ百科事典](https://dic.pixiv.net/a/AWOKE))

**Not a maronite 気づいてよ**
- [
DOLLCHESTRA 「AWOKE」 リリックビデオ (ラブライブ！蓮ノ空女学院スクールアイドルクラブ)(ドルケストラ)](https://www.youtube.com/watch?v=KnnSr-kI5sk)

## 概要

P2P地震情報のWebSocketから気象庁の地震情報、津波情報、緊急地震速報（警報）を受信し、Discord Webhookへ通知するサービス。

本書は仕様と実装・運用方法を記載する。P2P地震情報は非公式の情報提供サービスであるため、防災判断には気象庁や自治体の公式情報を優先する。緊急地震速報は予測を含み、観測された震度とは区別する。

## 動作環境

- Rocky Linux 9
- 固定グローバルIP、ドメインなし
- P2P地震情報とDiscordへのアウトバウンド通信およびDNS名前解決が必要
- 地図の日本語表示には日本語フォントが必要。Rocky Linux 9では `google-noto-sans-cjk-jp-fonts` をインストールする（提供リポジトリが無効な場合は管理者が有効化する）。
- 外部公開ポート、ドメイン、固定グローバルIPは本サービスの動作には不要

## 構成案

```text
P2P地震情報 WebSocket → 常駐サービス → Discord Webhook
                            ↕
                          SQLite
```

- Pythonで実装し、systemdで常駐・自動起動する。
- アプリケーションのソースは`src/awoke/`、テストは`tests/`に置く。依存関係とPythonバージョンは`pyproject.toml`で管理する。
- SQLiteに通知済み情報、情報の種類・識別子とDiscordのメッセージIDの対応、および送信・編集待ち情報を保存する。
- `/etc/awoke/config.toml`で551の震度閾値（初期値4）やログレベルなどを設定する。Webhook URLは設定ファイルに含めず、systemd credentials経由で読み込む。
- リリースファイルをサーバーへコピーして`/opt/awoke/`へ配置・更新する。更新時も`/etc/awoke/`の設定・credentialと`/var/lib/awoke/`のSQLiteデータは維持する。
- SQLiteの運用データは最終更新から7日間保持し、期限を過ぎたデータを定期的に削除する。バックアップは作成しない。
- 専用のsystem user `Awoke`で実行し、Webhook URLはsystemd credentials経由でアプリケーションへ渡す。URLを環境変数、コマンド引数、ログ、Gitには含めない。

## 受信情報

- WebSocket: `wss://api.p2pquake.net/v2/ws`。配信される情報のうち、次の3種類を処理する。
  - `551`: 観測後の地震情報
  - `552`: 津波警報・注意報などの津波情報
  - `556`: 緊急地震速報（警報）。すべての緊急地震速報（予報）を受信するものではない。
- 過去情報のHTTP API: `https://api.p2pquake.net/v2/history`。`codes`を551・552・556で複数指定し、最新100件を取得する。初回起動・再起動時に通知対象外の基準を作成するために利用する。
- `id`は受信した各報告の識別子として保存する。同一地震の複数報告は異なる`id`を持つことがある。追加した551サンプルでは、同じ`earthquake.time`の地震について`ScalePrompt`、`Destination`、`DetailScale`の報告が別々の`id`で配信された。
- 551の`issue.type`は報告内容を示し、地震の識別子ではない。`ScalePrompt`は震度速報、`Destination`は震源情報、`ScaleAndDestination`は震度と震源、`DetailScale`は各地の震度、`Foreign`は遠地地震、`Other`はその他の情報として扱う。訂正内容は`issue.correct`も参照する。
- **551の地震単位への統合（仮仕様）**: 各報告は`id`で個別に記録し、異なる`id`を同一地震状態へ統合する。同一地震の候補はまず`earthquake.time`で絞り、既に有効な震源座標がある場合は座標の整合性も確認する。現実装では距離100km以内を座標整合の暫定条件とする。座標未発表の速報は、同じ発生時刻の候補が一つだけで矛盾がない場合に限り統合する。候補が複数ある場合や震源情報が矛盾する場合は自動統合せず、ログに記録する。`earthquake.time`は単独では一意キーとみなさない。この照合距離は実データで検証して調整する。
- `issue.type`に応じて、その報告に含まれる情報だけを地震状態へ反映する。例えば`Destination`で既知の震度情報を消さず、`DetailScale`で震源座標を無効値に置き換えない。訂正報は`issue.correct`と報告時刻を参照して対象項目を更新する。`Foreign`、`Other`や未知の型は項目を確認できた範囲だけ反映する。
- 同じコード・`id`を再受信した場合は同一報告の再配信として扱う。異なる`id`の報告を既存の地震状態・Discordメッセージに結び付ける条件は、上記の候補が一意で矛盾しない場合に限る。
- 地域・観測点ごとの震度は、発表内容に応じて`points`に含まれる。地域と観測点は`isArea`で区別する。

## サンドボックスを使ったテスト

### 1. 準備

テスト中は**実際の地震情報を受信しない**。サンドボックスの過去情報は、設定済みのDiscord Webhookに送信される。必要に応じてサービス停止中にテスト専用Webhookに変更する。Webhook URLはログやチャットに貼らない。

バージョン0.2.0以降をサーバーに配置・インストールする。既存環境の更新方法は「障害対応・運用」のリリース更新手順を参照する。インストール済みのバージョンは次のコマンドで確認できる。

```sh
/opt/awoke/venv/bin/python -c 'from awoke import __version__; print(__version__)'
```

### 2. テストモードに切り替える

サービスを停止し、設定を編集する。

```sh
sudo systemctl stop awoke.service
sudo vi /etc/awoke/config.toml
```

`[awoke]`内の`test_mode`を`true`に変更する。項目がなければ追加する。`[awoke]`や`test_mode`を重複して追加しない。

```toml
[awoke]
test_mode = true
```

`vi`では`i`で編集し、編集後に`Esc`、`:wq`、Enterで保存する。保存後、読み込まれるモードとSQLiteのパスを確認する。

```sh
sudo /opt/awoke/venv/bin/python -c 'from pathlib import Path; from awoke.config import Config; c = Config.load(Path("/etc/awoke/config.toml")); print("test_mode =", c.test_mode); print("database =", c.effective_database)'
```

`test_mode = True`と表示されることを確認し、確認開始時刻を記録してからサービスを起動する。ログはその時刻以降のものだけを表示する。

```sh
awoke_log_since=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl start awoke.service
sudo systemctl status awoke.service --no-pager -n 0
sudo journalctl -u awoke.service --since "$awoke_log_since" --no-pager
```

### 3. 動作を確認する

ログに`Test mode enabled`と`Connected to P2P WebSocket mode=test`が出ていることを確認する。Discordには「【テスト】」を含むメッセージが届く。サンドボックスは約30秒間隔で過去の情報を配信するが、551は設定した震度閾値を満たす場合のみ通知するため、最初の情報では通知されないこともある。配信内容によっては既存メッセージが編集される。

通知が届かない場合は以下でコマンド実行時から新しく出力されるログを追跡し、接続エラー、Discordへの送信エラー、地図生成エラーなどを確認する。過去のログは表示しない。終了は`Ctrl+C`。

```sh
sudo journalctl -u awoke.service --since now -n 0 -f
```

### 4. 本番モードに戻す

サービスを停止し、設定ファイルの`[awoke]`内を`test_mode = false`に戻して保存する。テスト専用Webhookに変更した場合は、サービス停止中に本番用Webhookも戻す。

```sh
sudo systemctl stop awoke.service
sudo vi /etc/awoke/config.toml
```

```toml
[awoke]
test_mode = false
```

保存したら確認開始時刻を記録して起動し、その時刻以降のログで`Connected to P2P WebSocket mode=production`を確認する。

```sh
awoke_log_since=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl start awoke.service
sudo systemctl status awoke.service --no-pager -n 0
sudo journalctl -u awoke.service --since "$awoke_log_since" --no-pager
```

### テストモードの動作

- テスト中はP2Pの`wss://api-realtime-sandbox.p2pquake.net/v2/ws`に接続する。過去のサンドボックスデータを使うため、受信時の7日間の期限判定は行わない。551の震度閾値や保存データの7日間の定期削除は通常どおり適用する。
- サンドボックスの報告に`id`がなく、文字列の`_id`がある場合は、テストモードに限り`_id`を報告の識別子として扱う。
- Discordの送信・編集内容と画像に「テスト」と明示する。送信先は**既存のWebhookと同じ**なので、必要に応じてテスト専用Webhookに変更してから起動する。
- SQLiteは設定された`database`の末尾に`.sandbox`を付けた別ファイルを使う。例えば既定値は`/var/lib/awoke/awoke.sqlite3.sandbox`で、本番データには書き込まない。
- テスト中は起動時・再接続時の履歴API照合を行わず、接続中に配信された情報を処理する。切断中の情報は補完されない。過去情報が繰り返し配信された場合、既存メッセージの編集として扱われる場合がある。

## 通知仕様案

### 情報ごとの通知条件

- **地震情報（551）**: 全国の最大震度が設定値以上の場合に通知する。震度の初期設定値は4とし、設定で変更可能にする。
  - 震度は `1 < 2 < 3 < 4 < 5弱 < 5強 < 6弱 < 6強 < 7` の順で比較し、`maxScale`を震度に対応付ける。震度情報なしや不明な値は震度0とみなさず、ログに記録する。
  - 発生時刻、震源地、最大震度、マグニチュード、深さ、取得できた場合は津波情報を含める。マグニチュードは「マグニチュード: M4.4」の形式で表示する。津波情報が不明の場合は「心配なし」と推測しない。
  - 発生時刻は分単位で「2023/05/11 18:52頃」のように表示する。地震の識別や保存には元の時刻を使用する。
  - 初報が設定値未満で未通知の場合でも、更新後に設定値以上になれば新規メッセージを送信する。通知後に設定値未満になった場合も既存メッセージを編集して訂正を反映する。
- **津波情報（552）**: 津波注意報・津波警報・大津波警報を、震度の設定値に関係なく通知する。取得できる予報区、警報・注意報の種類、予想される高さ、到達予想時刻などを表示する。
  - 本文は大津波警報・津波警報・津波注意報の順に区分し、地域ごとに「予報区名・予想高さ・到達予想時刻」をタブ区切りで表示する。到達状況がある場合も時刻と合わせて表示し、未取得の高さや時刻・状況は「不明」とする。種別不明の地域も省略しない。
  - 地震ごとの識別はせず、全解除までの全国の発表を一連の情報として扱う。初回発表は新規投稿し、地域追加や警報の引き上げ、到達状況・予想高さの変更は最新投稿を編集する。
  - 地域の削除またはいずれかの地域での警報の引き下げ時は、直前の投稿を残して新規投稿する。拡大・引き上げが同時にある場合も新規投稿する。比較順序は津波注意報、津波警報、大津波警報とする。種別不明の場合は大小を推定せず警告する。残る地図は縮小直前の状態であり、全期間の最大範囲を合成したものではない。
  - 全解除は`cancelled`で判定する。縮小・引き下げによる新規投稿がなければ解除用の新規投稿を作成し、あれば最新投稿を解除表示へ編集する。それまで残した投稿の本文に「（解除済）」を追記し、画像は変更しない。解除済み表示の編集に失敗した場合はログに記録し、後続の津波報受信時に再試行する。
  - `issue.time`の発表順で管理し、古い報告や同時刻・同内容の再配信は反映しない。同時刻で内容が異なる場合は警告して反映しない。有効な発表時刻、`cancelled`、地域一覧が欠ける報告は警告して処理しない。解除後は、解除報より新しい発表を新規の一連の情報として扱う。
- **緊急地震速報（556）**: 警報を震度の設定値に関係なく通知する。本文には取得できる震源、マグニチュード、予想最大震度を表示し、予測であることを明記する。発表地域の一覧は本文に含めず、地図画像の左側に表示する。
  - 予想最大震度は受信した発表地域の`scaleTo`（予想震度の上限）の最大値とする。上限が「以上」の場合は`scaleFrom`（下限）を考慮して「以上」と表示し、上限が不明な地域が含まれる場合はその旨を付記する。すべて不明なら「不明」とする。
  - `issue.eventId`を地震単位のキー、`issue.serial`を報の順序として扱う仮仕様で実装する。同一`eventId`の続報は状態を更新して既存メッセージを編集し、古いserialは状態に反映しない。取消は`cancelled`を確認して反映する。フィールド欠落や矛盾がある場合は誤結合せずログに記録する。この仮仕様は実データで検証して調整する。

### 共通の扱い

- 速報（556）と観測後の地震情報（551）は、同じ地震に関するものでも別メッセージにする。津波情報（552）も別メッセージにする。
- 初回起動・再起動時は、まずWebSocketへ接続して受信情報を一時保留し、その後に過去情報APIを取得する。履歴に含まれる情報は通知せず、以後の更新判定の基準として保存する。過去情報API取得中に保留したWebSocket情報は履歴と照合してから処理し、起動時に過去情報を一斉通知しない。既存の通知済み情報に対応するDiscordメッセージIDは再起動後も維持し、必要な更新はそのメッセージへ反映する。初期化完了後に届いた新しい情報は通常どおり通知する。
- WebSocket再接続後も履歴APIと照合し、保存済みでない期間中の報告は通知対象として処理する。履歴にある既知報告は状態を照合するだけで、新規通知を重複送信しない。
- 初報の送信時はWebhookに`wait=true`を指定し、551は地震単位の状態キーとDiscordのメッセージIDを、個々のP2P報告`id`との対応と合わせてSQLiteに保存する。552は発表時刻と一連の投稿ID・内容を保存し、556は`issue.eventId`を基本キーとして各報告の`id`と`issue.serial`も保存する。保存期限が過ぎて削除された情報は復元できない。
- 既存バージョンで報告IDごとに投稿された552メッセージは、新しい一連の投稿管理には自動移行しない。アップデート後は履歴照合または新たな受信情報から状態を作成する。
- 551の報告が一意な既存地震に統合された場合は、その地震に対応するDiscordメッセージを編集して統合後の状態を反映する。候補が曖昧、または震源情報が矛盾する場合は誤編集を避けて統合せず、ログに記録する。556は同じ`issue.eventId`の続報を既存メッセージへ反映する。対応するメッセージがまだない場合、551は現在の最大震度が閾値以上なら新規送信し、552・556は新規メッセージを送信する。551で初報が閾値未満だった場合も、後続更新が閾値以上になった時点で新規送信する。
- 既存メッセージを編集できない場合は、その旨をログに記録し、最新内容で新規メッセージを投稿する。
- メッセージの編集は新規メッセージではないため、Discordで再度通知されることは期待しない。

## 地図表示仕様案

- 背景の陸地には気象庁の「地震情報／細分区域」GISを使用する。地震・緊急地震速報では塗り分けと同じ図形を使い、異なる海岸線データを重ねることによるずれを抑える。GISの対象外の陸地は表示されない。
- 津波情報は同じ陸地の下地に別の「津波予報区」GISの線を重ねる。対象の海岸線は津波注意報を黄色（`#FFFF00`）、津波警報を赤色（`#FF0000`）、大津波警報を紫色（`#960096`）の太線で描き、開いた線の両端は結ばない。地図内に津波警報・注意報の文字は表示せず、種類は左側の一覧に表示する。線が重なる場合は重大な警報を優先する。未知の種類は推測して塗らず、ログに記録する。データ間の海岸線のずれが残る場合がある。
- 全解除時は北海道から九州を含む固定範囲の地図を作成し、警報色を描かず、左側に「津波警報・注意報はすべて解除されました」と表示する。
- 既存の`land_shapefile`設定は互換性のため読み込むが、地図描画には使用しない。
- メモリ消費を抑えるため、区域の属性情報のみをキャッシュし、図形は対象区域と画像の余白を含む表示範囲に重なる区域・陸地だけを読み込む。使用量は対象範囲や地図データによって変わる。
- 海と画像背景は暗色、陸地は灰色、海岸線と区域の境界線は明るい灰色で描画する。震度情報がなく塗り分けのない区域にも境界線を表示する。地震情報は観測震度別の色、緊急地震速報は警報発表地域を予想震度に関係なく赤色で表示する。緊急地震速報では予想震度が不明でも、区域名がGISと照合できれば地図を表示する。
- P2P地震情報の震源座標や地域別情報を背景地図に重ね、地図画像を生成してDiscordのメッセージに添付する。地図範囲は発表対象地域と震源が収まるように切り取り、見切れ防止の余白を設ける。広範囲では地理的な余白の増加に上限を設け、対象範囲の縦横比に応じて地図の幅を狭めて過大な余白を抑える。震源座標が不明な場合は震源を表示せず、発表対象地域を基準に範囲を決める。
- 最終画像は1920×1080ピクセルとする。左側480ピクセルに凡例・観測最大震度・地域一覧を配置し、右側1440ピクセルを地図領域とする。地図を引き伸ばしたり切り取ったりせずに表示範囲を調整し、余白は海の背景色で補う。
- 地図は球面メルカトル図法で描画し、陸地・区域・震源・ラベルに同じ変換を適用する。極付近の緯度は図法の発散を避けるため南北約85.051度に制限する。
- 地域境界には事前にダウンロードした気象庁のGISデータを利用する。551と556は「地震情報／細分区域」、552は「津波予報区」を使う。552と556は受信した地域名をGIS属性名と直接照合する。
- 551の`points`は発表種別により地域単位と観測点単位がある。`isArea: true`の地域名は「地震情報／細分区域」の属性名と照合して塗り分ける。確認済みサンプルの`ScalePrompt`では6地域すべてが境界データに一致した。
- 気象庁の区域名称一覧では、緊急地震速報の区域は「府県予報区」と、その下位の「緊急地震速報や震度情報で用いる区域名」に分かれる。556の`areas[].name`で確認した5地域（愛知県西部、静岡県西部、愛知県東部、静岡県中部、岐阜県美濃東部）は下位の区域名に該当し、「地震情報／細分区域」GISの名称・コードとも一致する。塗り分け時はこの区域名でGISへ直接照合し、府県予報区名へ置き換えない。別の地域名が届いた場合は推測せず、未対応として記録する。
- `DetailScale`など観測点単位（`isArea: false`）の震度は、[対応表](./data/area_mapping.csv)を使って地域に集約する。同一地域内では観測震度の最大値を使う。
- 対応表にない観測点は警告を記録し、その地点だけを地図から除外する。対応する地点が一つもない場合は画像を添付せず、地震情報はテキストで通知する。地図の地域別震度は対応できた地点のみの最大値であり、未対応地点を含む地域全体の最大値とは限らない。
- 対応表には提供された[観測点データ](./data/area_mapping.json)の4,360地点を収録する。データ内の区域コードと区域名は、地震情報の細分区域GISと照合済み。既存の地域単位の対応も保持する。552・556は受信名をGISの区域名と直接照合する。未対応名が届いた場合は推測で割り当てず、照合方法を検証する。
- 対応表を更新する場合は、`python tools/update_area_mapping.py data/area_mapping.json data/area_mapping.csv <細分区域のshpファイル>`を実行する。既存の地点と矛盾する場合や区域コード・名称がGISと一致しない場合は更新を中止する。リリースには実行時に必要なCSVを含める。
- 地図には地域単位の値を表示する場合「各地域内で観測された最大震度」と明記する。震度情報がない地域は震度0とみなさず、未着色またはデータなしとして表示する。
- 地図上に地域名は表示しない。地震情報と緊急地震速報では対象地域内に震度配色の縁付き正方形を配置し、震度を`1`〜`4`、`5-`、`5+`、`6-`、`6+`、`7`で表示する。予想震度が不明な地域には記号を表示しない。記号が重なる場合は震度の高い地域を優先し、表示できない区域も左側の一覧には掲載する。赤い×は震源を表す。地震情報の震度の配色凡例は左側の一覧の上に表示し、地図には重ねない。緊急地震速報の地域は警報色で塗るため震度別の塗り分け凡例を表示しない。一覧は震度ごとにまとめて高い順に並べ、各グループ内を3列に配置する。地域が多い場合や地域名が長い場合は一覧だけを縮小して左側480ピクセルに収め、画像全体の寸法と地図領域を維持する。津波予報区の地図上には警報・注意報の文字を表示せず、種類は左側の一覧で示す。日本語フォントがない場合は文字化けした画像を送信せず、ログにエラーを記録してテキストのみ通知する。
- 地震情報の震度の塗り分けには、次のRGB値を使う。凡例は3列×3行で表示し、色だけに頼らず震度の文字も表示する。

  | 震度 | RGB | HEX |
  |---|---|---|
  | 7 | `150, 0, 150` | `#960096` |
  | 6強 | `160, 0, 0` | `#A00000` |
  | 6弱 | `230, 0, 0` | `#E60000` |
  | 5強 | `255, 120, 0` | `#FF7800` |
  | 5弱 | `255, 180, 0` | `#FFB400` |
  | 4 | `250, 250, 100` | `#FAFA64` |
  | 3 | `0, 200, 200` | `#00C8C8` |
  | 2 | `30, 110, 230` | `#1E6EE6` |
  | 1 | `70, 100, 110` | `#46646E` |
- 観測震度と予想震度、津波情報は区別して表示する。津波予報区などの地域を正確に対応付けられない場合も、誤った塗り分けを行わずログに記録する。
- 更新報では地図画像も更新し、既存のDiscordメッセージに反映する。添付画像の更新方法はDiscordの仕様に従う。
- **出典・利用条件**:
  - 気象庁GISは気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に従い、出典と加工した旨を表示する。地図画像には「出典：気象庁 予報区等GISデータを加工して作成」と記し、[GISデータ一覧](https://www.data.jma.go.jp/developer/gis.html)のURLを表示する。完成した地図が気象庁作成と誤認されないようにする。
  - 気象庁コンテンツの出典・加工表示と利用上の注意を守る。気象業務法など個別法令の制約も確認し、気象庁の公式情報と誤認される表現を避ける。
- 別データ間の海岸線や境界のずれ、島の欠落も検証する。
- 地図データの読み込みや画像生成に失敗した場合はエラーを記録し、地震・津波情報の通知自体はテキストで行う。

## 障害対応・運用

- **実行ユーザー**: ログインシェルを`/sbin/nologin`にしたsystem user/group `Awoke`を作成し、サービス専用にする。sudo権限は与えない。
- **配置と権限**:
  - アプリケーションとPython仮想環境: `/opt/awoke/`。`root:root`所有で、サービスからは読み取り・実行のみ。
  - SQLiteと状態ファイル: `/var/lib/awoke/`。systemdの`StateDirectory=awoke`で作成し、`Awoke:Awoke`所有、ディレクトリ権限`0750`とする。
  - 設定: `/etc/awoke/config.toml`。`root:Awoke`所有、`0640`。Webhook URLなどの秘密値はここに書かない。
  - Webhook URL: `/etc/awoke/discord-webhook-url`。`root:root`所有、`0600`。`https://discord.com/api/webhooks/`および`https://canary.discord.com/api/webhooks/`で始まるURLに対応する。systemdの`LoadCredential=discord-webhook-url:/etc/awoke/discord-webhook-url`でサービス起動時に渡し、アプリは`CREDENTIALS_DIRECTORY`内のcredentialファイルを読む。設定変更後はサービスを再起動して反映する。
- **systemd**: システムサービスとして自動起動し、`User=Awoke`、`Group=Awoke`、`WorkingDirectory=/opt/awoke`を指定する。異常終了時は`Restart=on-failure`、`RestartSec=5s`で再起動し、終了時は最大30秒の猶予を与えてWebSocket切断とSQLiteの終了処理を行う。`NoNewPrivileges=true`、`PrivateTmp=true`、`ProtectSystem=strict`、`ProtectHome=true`などで権限を制限し、外部通信に必要なアドレスファミリーだけ許可する。unitファイルの`ExecStart`は実装後の起動方法に合わせて確定する。
- **ログ**: アプリケーションは標準出力・標準エラーへ構造化ログを出し、journaldで管理する。確認は`journalctl -u awoke.service`を使う。Webhook URLやcredential内容、認証ヘッダーは絶対に出力せず、障害調査に不要な受信JSON全体も記録しない。
- **日本語フォント**: Rocky Linux 9では`sudo dnf install --enablerepo=crb google-noto-sans-cjk-jp-fonts`を実行し、`/usr/share/fonts/google-noto-cjk/NotoSansCJKjp-Regular.otf`が読み取れることを確認する。`--enablerepo=crb`はこの実行時のみCRBを有効にする。パッケージが見つからない場合は、利用可能なリポジトリを管理者に確認する。フォント導入後はサービスを再起動する。
- **データ保護**: SQLiteの運用データは最終更新から7日経過後に削除する。バックアップは作成しないため、ディスク障害などでは通知履歴やメッセージ対応情報を失う可能性がある。
- 7日間の保存期限を過ぎて対応情報が削除された後に同じ情報の更新報が届いた場合は、新規投稿や既存メッセージの編集を行わず、期限切れ情報としてログにのみ記録する。
- **リリース作成**: リポジトリルートで`python tools/create_release.py <出力ディレクトリ>`を実行する。ソース、依存定義、551対応表、使用中のJMA GISシェープファイルをZIPにまとめる。サンプルJSONは含めない。Windows PowerShellでは、例えば`python tools/create_release.py C:\Temp\awoke-release`を実行し、生成された`C:\Temp\awoke-release\awoke-0.3.0b1.zip`をサーバーへ転送する。エクスプローラーでリポジトリフォルダーを直接ZIP化したものはリリースZIPではない（`.git`フォルダーが含まれている場合は特に異なる）。
- **Rocky Linux 9への初回配置**: ZIPを一時ディレクトリへ展開し、ZIP内の`pyproject.toml`を含むディレクトリの内容を`/opt/awoke/`へコピーする（`pyproject.toml`が`/opt/awoke/pyproject.toml`、地図データが`/opt/awoke/share/`となる配置）。ZIPのバージョン名を固定せず、展開後にファイルから配置元を特定する:

  ```sh
  mkdir -p /tmp/awoke-release
  unzip -q /path/to/awoke-VERSION.zip -d /tmp/awoke-release
  project_file=$(find /tmp/awoke-release -mindepth 1 -maxdepth 3 -type f -name pyproject.toml -print -quit)
  test -n "$project_file" || { echo "pyproject.toml not found in the release ZIP" >&2; exit 1; }
  release_dir=${project_file%/pyproject.toml}
  test -d "$release_dir/share" || { echo "Release map data directory not found" >&2; exit 1; }
  sudo mkdir -p /opt/awoke
  sudo cp -a "$release_dir/." /opt/awoke/
  sudo chown -R root:root /opt/awoke
  ```

  Python 3.11以上とsystem user/group `Awoke`が必要。各段階でエラーが出た場合は先に進まず、原因を確認する。仮想環境の作成とインストール:

  ```sh
  sudo python3.11 -m venv /opt/awoke/venv
  sudo /opt/awoke/venv/bin/pip install /opt/awoke
  ```

  設定ファイルが存在しない場合のみサンプルをコピーし、所有者と権限を設定する。Webhook credentialは変更しない:

  ```sh
  sudo mkdir -p /etc/awoke
  if sudo test -e /etc/awoke/config.toml; then
    echo "Existing config.toml preserved"
  else
    sudo cp -n /opt/awoke/config.example.toml /etc/awoke/config.toml
  fi
  sudo chown root:Awoke /etc/awoke/config.toml
  sudo chmod 0640 /etc/awoke/config.toml
  sudo stat -c '%U:%G %a %n' /etc/awoke/config.toml /etc/awoke/discord-webhook-url
  ```

  Webhook credentialが`root:root`・`0600`であることを確認し、systemdにサービスを登録・起動する:

  ```sh
  sudo cp /opt/awoke/systemd/awoke.service /etc/systemd/system/awoke.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now awoke.service
  sudo systemctl status awoke.service --no-pager
  ```
- **リリース更新**: 新しいリリースZIPをサーバーへ転送し、新しい一時ディレクトリへ展開する。下記は`0.3.0b1`の例なので、更新するZIPと展開先のバージョンを実際のものに合わせる。エラーが出た場合は先に進まない。

  ```sh
  release_tmp=$(mktemp -d /tmp/awoke-update.XXXXXX)
  unzip -q /tmp/awoke-0.3.0b1.zip -d "$release_tmp"
  release_dir="$release_tmp/awoke-0.3.0b1"
  ls -ld "$release_dir/pyproject.toml" "$release_dir/share" "$release_dir/systemd/awoke.service"
  ```

  展開内容を確認してからサービスを停止し、リリースをコピーして仮想環境へインストールする。`/etc/awoke/`の設定・credentialと`/var/lib/awoke/`のSQLiteは変更しない。

  ```sh
  sudo systemctl stop awoke.service
  sudo cp -r --no-preserve=ownership "$release_dir/." /opt/awoke/
  sudo /opt/awoke/venv/bin/pip install --upgrade /opt/awoke
  sudo /opt/awoke/venv/bin/python -c 'from awoke import __version__; print("Installed version:", __version__)'
  sudo cp /opt/awoke/systemd/awoke.service /etc/systemd/system/awoke.service
  sudo systemctl daemon-reload
  sudo restorecon -RF /opt/awoke
  ```

  **0.2.3以前からの更新時**は、日本語ファイル名の文字化けを避けるために地図ファイルを英数字の名前に変更している。既存の`/etc/awoke/config.toml`を上書きせず、`sudo vi /etc/awoke/config.toml`で`[awoke]`内の次の2行を変更する。同じキーを重複させず、他の設定はそのまま残す。

  ```toml
  seismic_shapefile = "/opt/awoke/share/20240520_AreaForecastLocalE_GIS/seismic.shp"
  tsunami_shapefile = "/opt/awoke/share/20240520_AreaTsunami_GIS/tsunami.shp"
  ```

  表示されたバージョンが`0.3.0b1`であることを確認する。設定保存後、地図ファイルを確認してサービスを起動する。起動前の時刻からログを表示する。

  ```sh
  sudo -u Awoke test -r /opt/awoke/share/20240520_AreaForecastLocalE_GIS/seismic.shp
  sudo -u Awoke test -r /opt/awoke/share/20240520_AreaForecastLocalE_GIS/seismic.dbf
  sudo -u Awoke test -r /opt/awoke/share/20240520_AreaTsunami_GIS/tsunami.shp
  sudo -u Awoke test -r /opt/awoke/share/20240520_AreaTsunami_GIS/tsunami.dbf
  awoke_log_since=$(date '+%Y-%m-%d %H:%M:%S')
  sudo systemctl start awoke.service
  sudo systemctl status awoke.service --no-pager -n 0
  sudo journalctl -u awoke.service --since "$awoke_log_since" --no-pager
  ```
- WebSocket切断時は、待機時間を徐々に延ばしランダムな揺らぎを加えて再接続する。接続監視はP2P地震情報の仕様に従い、地震情報が届かないことだけを切断と判断しない。
- 再接続後はHTTP APIの履歴と保存済み情報を照合し、停止中の新規情報を可能な範囲で補完する。取得可能な履歴には制限があり、停止中の全情報の復元は保証できない。
- APIのレート制限と利用条件を守り、不要な接続や履歴取得を避ける。
- Discordへの一時的な送信失敗が明確な場合は再試行する。HTTP `429`ではDiscordが示す待機時間に従う。Webhook無効などの恒久的なエラーは連続送信せず、ログや監視で検知できるようにする。
- Discordへの送信結果が不明な場合は重複通知を避けるため再試行せず、通知漏れの可能性を許容してログに記録する。
- 長時間の切断や送信失敗は、可能ならDiscord以外の経路でも監視する。

### Discord Webhook URLの変更

1. Discordで変更先のWebhook URLを取得する。URLは秘密情報なので、チャット、Git、シェルのコマンド引数や履歴に記載しない。端末の画面共有・操作記録にも注意する。
2. サービスを停止し、credentialファイルを編集する。停止中はリアルタイム受信できない。

   ```sh
   sudo systemctl stop awoke.service
   sudoedit /etc/awoke/discord-webhook-url
   ```

3. エディタでファイルの内容を**新しいWebhook URLだけ**に置き換えて保存する。`https://discord.com/api/webhooks/`または`https://canary.discord.com/api/webhooks/`で始まるURLを使用する。`sudoedit`は編集用の一時ファイルを作成するため、信頼できる環境で実行し、エディタのバックアップや復元ファイルにもURLを残さないようにする。
4. 所有者と権限を設定し、URLを表示せずに確認する。エラーが出た場合は先に進まない。

   ```sh
   sudo chown root:root /etc/awoke/discord-webhook-url
   sudo chmod 0600 /etc/awoke/discord-webhook-url
   sudo stat -c '%U:%G %a %n' /etc/awoke/discord-webhook-url
   ```

   `root:root 600`と表示されることを確認する。
5. サービスを起動し、状態と起動後のログを確認する。変更したcredentialは起動時に読み直される。

   ```sh
   awoke_log_since=$(date '+%Y-%m-%d %H:%M:%S')
   sudo systemctl start awoke.service
   sudo systemctl status awoke.service --no-pager -n 0
   sudo journalctl -u awoke.service --since "$awoke_log_since" --no-pager
   ```

   `active (running)`を確認し、次の通知が変更先に届くことを確認する。起動成功だけではWebhookへの送信成功は保証されない。ログを共有する場合は秘密情報が含まれないことを確認する。

別のWebhookに変更すると、以前のWebhookが作成したメッセージは新しいWebhookでは編集できない。続報の編集に失敗した場合、最新内容が新規投稿されることがあり、過去の津波投稿への「解除済」追記も失敗する可能性がある。停止中の情報は履歴から可能な範囲で照合するが、すべての復元は保証されない。テストモードでは履歴照合を行わない。

## 検証項目

- 震度の正規化と、設定値ちょうど・設定値超過の境界判定
- 初回起動時の過去情報の扱い、仮仕様による`issue.type`ごとの段階的な情報統合、同一発生時刻の別地震を誤結合しない判定
- 初回の基準作成中に届く情報と、履歴の照合による復旧
- 通知済みメッセージの編集、震度の閾値をまたぐ更新、編集できない場合の扱い
- 551と556を別メッセージにすること、551の地震統合、556の`eventId`・`serial`による更新・取消、552の更新・解除
- 地図への震源・地域情報の重ね合わせ、画像更新、地図データの読み込みや画像生成の失敗時のテキスト通知
- WebSocket切断、再接続、サービス再起動後の復旧
- Discordの送信失敗・レート制限時の再試行
- Rocky Linux 9でのsystemd自動起動とテスト用Webhookへの送信

## 参考資料

- [P2P地震情報 API仕様](https://www.p2pquake.net/develop/json_api_v2/)
- [P2P地震情報の二次利用](https://www.p2pquake.net/secondary_use/)
- [気象庁 予報区等GISデータ](https://www.data.jma.go.jp/developer/gis.html)
- [気象庁ホームページの利用規約](https://www.jma.go.jp/jma/kishou/info/coment.html)
- [気象庁 緊急地震速報や震度情報で用いる区域の名称](https://www.jma.go.jp/jma/kishou/know/jishin/joho/shindo-name.html)
- [気象庁 震度情報で用いる区域名・市町村名](https://www.data.jma.go.jp/eqev/data/joho/shindo-name.html)
- [Discord Webhook API](https://docs.discord.com/developers/resources/webhook)
- [Discord APIのレート制限](https://docs.discord.com/developers/topics/rate-limits)