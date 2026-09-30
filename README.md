<p align="center">
  <img src="assets/cerbero-logo.svg" alt="Logo de Cerbero: un perro de tres cabezas con collar de púas bajo la lluvia" width="128">
</p>

<h1 align="center">Cerbero</h1>

Monitor de seguridad y estado de **server-kuro**. Vigila tres frentes a la vez —recursos del
host, contenedores Docker y accesos SSH— y avisa por Telegram cuando algo va mal. No sustituye
a Netdata en métricas detalladas: es la capa propia que solo avisa de lo que importa, sobre
todo en seguridad. Expone una API REST que consume [Dis](https://github.com/kur0h3i/Dis).

> Cerbero es el perro de tres cabezas que guarda el tercer círculo del Infierno; aquí guarda
> el servidor ([por qué el nombre](#el-nombre-y-el-logo)).

> **En construcción.** Orden de implementación: núcleo (config, almacén de alertas y
> Telegram) → cabeza Recursos → cabeza Contenedores → cabeza Accesos → API → Docker.

## El nombre y el logo

> *Cerbero, fiera crudele e diversa,*
> *con tre gole caninamente latra*
> *sovra la gente che quivi è sommersa.*
>
> — Dante, *Infierno*, VI, 13-15

Cerbero guarda el tercer círculo del *Infierno* de Dante, el de los golosos, y ladra con sus
tres gargantas a quien se acerca. Aquí cada cabeza vigila un frente: recursos, contenedores y
accesos. El logo, en la misma paleta que Dis y Caronte, dibuja el Canto VI:

- **Las tres cabezas** en el violeta de Dis, con el borde al rojo vivo.
- **«Li occhi ha vermigli»** (v. 16): los ojos de brasa.
- **El collar de púas**, del mismo fuego que las murallas de Dis.
- **La lluvia eterna** del tercer círculo, «grandine grossa, acqua tinta e neve» (v. 10), en
  el lila de la Estigia.

`assets/cerbero-logo.svg` es el logo sin fondo; `assets/cerbero.svg`, la variante con fondo
oscuro redondeado para usar como icono.
