import mysql.connector
from mysql.connector import Error

connection = None
cursor = None

try:
    connection = mysql.connector.connect(
        host='127.0.0.1',
        port=3306,
        database='deneme_db',
        user='root',
        password='QvXdb16az.'  # Şifreni buraya gir
    )

    if connection.is_connected():
        # Düzeltme: get_server_info() yerine server_info kullanıyoruz
        db_info = connection.server_info
        print(f"MySQL Sunucusuna bağlandı (Sürüm: {db_info})")

        cursor = connection.cursor()

        # 1. Adım: Veritabanındaki tüm tabloları al
        cursor.execute("SHOW TABLES;")
        tables = cursor.fetchall()  # Bu, [('tablo1',), ('tablo2',)] gibi döner

        if not tables:
            print(f"'{connection.database}' veritabanında hiç tablo bulunamadı.")
        else:
            print(f"\n'{connection.database}' veritabanındaki tablolar:")

            # 2. Adım: Her tablo için bir döngü başlat
            for table_tuple in tables:
                table_name = table_tuple[0]  # Tuple'ın içinden asıl ismi al
                print(f"\n--- '{table_name}' Tablosunun İçeriği ---")

                try:
                    # Tablo adları parametre olarak (%s) KULLANILAMAZ.
                    # Ancak bu tablo adını 'SHOW TABLES' ile biz kendimiz
                    # aldığımız için f-string burada GÜVENLİDİR.
                    # (Backtick ` kullanmak iyi bir alışkanlıktır,
                    # 'order' gibi rezerve kelimelerle çakışmaz)
                    cursor.execute(f"SELECT * FROM `{table_name}`")

                    rows = cursor.fetchall()

                    if not rows:
                        print("(Bu tablo boş.)")
                    else:
                        # (Daha okunaklı olması için kolon adlarını da alalım)
                        column_names = [desc[0] for desc in cursor.description]
                        print(f"Kolonlar: {column_names}")

                        # Tüm satırları yazdır
                        for row in rows:
                            print(row)

                except Error as e_table:
                    # Bazı tabloları (örn: VIEW) okuma izni olmayabilir
                    print(f"'{table_name}' tablosu okunurken hata: {e_table}")

except Error as e:
    print(f"MySQL bağlantı hatası: {e}")

finally:
    # Bağlantıyı her zaman kapat
    if cursor:
        cursor.close()
    if connection and connection.is_connected():
        connection.close()
        print("\nMySQL bağlantısı kapatıldı.")